import os
import re
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from sheets import get_spreadsheet, ensure_tabs, cleanup_expired_jobs, read_jobs, save_jobs, compact_blank_rows, remove_duplicate_rows, job_key
from workday import pretty_company, confirm_sites, discover_workday_sites, enrich_workday_job, fetch_workday_jobs
from discovery import discover_workday_sites_from_search, COMPANY_NAMES
from matcher import evaluate_job, now_ist, rule_based_evaluate, QuotaExhausted
from pypdf import PdfReader

load_dotenv()
ROOT = Path(__file__).resolve().parent

def read_config_text(filename):
    path = ROOT / filename
    if not path.exists():
        raise RuntimeError(f"Missing {filename}. Add it to the GitHub repository.")
    return path.read_text(encoding="utf-8").strip()

def read_resume_pdf():
    path = ROOT / "resume.pdf"
    if not path.exists():
        raise RuntimeError("Missing resume.pdf. Upload your resume PDF to the repository root, beside scanner.py.")
    try:
        reader = PdfReader(str(path))
        pages = []
        for page in reader.pages:
            pages.append(page.extract_text() or "")
        resume_text = "\n".join(pages).strip()
    except Exception as exc:
        raise RuntimeError(f"Could not read resume.pdf: {exc}") from exc
    if len(resume_text) < 80:
        raise RuntimeError(
            "Could not extract enough text from resume.pdf. Upload a text-selectable/searchable PDF, "
            "not a scanned image-only PDF."
        )
    print(f"Resume loaded from resume.pdf ({len(resume_text)} characters).")
    return resume_text

def is_clearly_outside_india(location):
    """Fast reject only locations explicitly identified as outside India.

    City/state-only and ambiguous locations are passed to the LLM after enrichment,
    so Indian states/cities do not get discarded just because the word India is absent.
    """
    text = (location or "").lower().strip()
    if not text:
        return False
    foreign_markers = [
        "united states", "u.s.a.", "usa", "united kingdom", "uk", "canada", "mexico",
        "brazil", "argentina", "colombia", "chile", "peru", "ecuador", "ireland",
        "germany", "france", "spain", "italy", "portugal", "netherlands", "belgium",
        "switzerland", "austria", "poland", "czech republic", "czechia", "hungary",
        "romania", "bulgaria", "greece", "sweden", "norway", "denmark", "finland",
        "australia", "new zealand", "singapore", "malaysia", "indonesia", "thailand",
        "vietnam", "philippines", "china", "japan", "south korea", "korea", "taiwan",
        "hong kong", "israel", "saudi arabia", "united arab emirates", "uae", "qatar",
        "south africa", "egypt", "kenya", "nigeria", "morocco", "turkey", "türkiye",
        "remote - us", "remote - usa", "remote - canada", "remote - uk", "remote - europe",
    ]
    # An explicit India mention overrides generic words that could occur in an address.
    if "india" in text or "indian" in text:
        return False
    return any(marker in text for marker in foreign_markers)

def is_clearly_outside_india(location):
    """Fast reject only locations explicitly identified as outside India.

    City/state-only and ambiguous locations are passed to the LLM after enrichment,
    so Indian states/cities do not get discarded just because the word India is absent.
    """
    text = (location or "").lower().strip()
    if not text:
        return False
    foreign_markers = [
        "united states", "u.s.a.", "usa", "united kingdom", "uk", "canada", "mexico",
        "brazil", "argentina", "colombia", "chile", "peru", "ecuador", "ireland",
        "germany", "france", "spain", "italy", "portugal", "netherlands", "belgium",
        "switzerland", "austria", "poland", "czech republic", "czechia", "hungary",
        "romania", "bulgaria", "greece", "sweden", "norway", "denmark", "finland",
        "australia", "new zealand", "singapore", "malaysia", "indonesia", "thailand",
        "vietnam", "philippines", "china", "japan", "south korea", "korea", "taiwan",
        "hong kong", "israel", "saudi arabia", "united arab emirates", "uae", "qatar",
        "south africa", "egypt", "kenya", "nigeria", "morocco", "turkey", "türkiye",
        "remote - us", "remote - usa", "remote - canada", "remote - uk", "remote - europe",
    ]
    # An explicit India mention overrides generic words that could occur in an address.
    if "india" in text or "indian" in text:
        return False
    return any(marker in text for marker in foreign_markers)


INDIA_WORDS = [
    "india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "mumbai", "navi mumbai",
    "gurgaon", "gurugram", "noida", "greater noida", "delhi", "new delhi", "kolkata", "ahmedabad",
    "mangalore", "mangaluru", "mysore", "mysuru", "kochi", "cochin", "coimbatore", "jaipur", "chandigarh",
    "indore", "trivandrum", "thiruvananthapuram", "nagpur", "bhubaneswar", "vadodara", "visakhapatnam",
    "lucknow", "surat", "bhopal", "patna", "mohali", "faridabad", "ghaziabad", "thane", "karnataka",
    "maharashtra", "telangana", "tamil nadu", "haryana", "uttar pradesh", "kerala", "gujarat",
    "west bengal", "andhra pradesh", "odisha", "rajasthan", "madhya pradesh", "punjab",
]
INDIA_RE = re.compile(r"\b(?:" + "|".join(re.escape(w) for w in INDIA_WORDS) + r")\b", re.I)
SENIOR_RE = re.compile(r"\b(senior|sr\.?|staff|principal|lead|manager|director|head of|vp|vice president|architect|specialist iii?|iv)\b", re.I)


def definitely_india(text):
    return bool(INDIA_RE.search(text or ""))


def maybe_india(location):
    """Cheap listing-level check: India text, or an ambiguous multi-location/remote listing."""
    t = (location or "").strip()
    return (not t) or definitely_india(t) or bool(re.search(r"\d+\s+locations?|\bremote\b", t, re.I))


def not_senior(title):
    return "intern" in (title or "").lower() or not SENIOR_RE.search(title or "")


def main():
    start = time.time()
    budget = float(os.environ.get("RUN_BUDGET_MIN", "270")) * 60   # stay under the 6h Actions limit
    fetch_budget = budget * 0.45
    max_llm = int(os.environ.get("MAX_LLM_CALLS", "5000"))
    llm_delay = float(os.environ.get("LLM_DELAY_SEC", "2"))
    terms = [t.strip() for t in os.environ.get("SEARCH_TERMS", "intern,graduate,trainee,2027").split(",") if t.strip()]
    elapsed = lambda: time.time() - start

    resume = read_resume_pdf()
    manual = [l.strip() for l in read_config_text("career_sites.txt").splitlines()
              if l.strip() and not l.strip().startswith("#")]
    discovered = discover_workday_sites_from_search(max_sites=int(os.environ.get("MAX_DISCOVERED_SITES", "5000")))
    candidates = list(dict.fromkeys(manual + discovered))
    if not candidates:
        raise RuntimeError("Discovery returned no candidate sites.")

    # Fast parallel validation (optionally probe extra site names with PROBE_SITES=1).
    sites = confirm_sites(candidates)
    if os.environ.get("PROBE_SITES") == "1":
        sites = discover_workday_sites(sites)
    print(f"{len(sites)} confirmed Workday site(s) of {len(candidates)} candidates ({elapsed()/60:.1f} min).")
    if not sites:
        raise RuntimeError("No working public Workday career sites were found.")

    spreadsheet = get_spreadsheet()
    ensure_tabs(spreadsheet)
    cleanup_expired_jobs(spreadsheet, max_age_days=3)
    remove_duplicate_rows(spreadsheet)
    compact_blank_rows(spreadsheet)
    _, sheet_rows, _ = read_jobs(spreadsheet)
    existing = {job_key(r.get("Application URL", "")) for r in sheet_rows}
    errors = []

    # Phase 1: fetch only intern/graduate/trainee listings, in parallel, keep India-ish ones.
    cap = int(os.environ.get("MAX_JOBS_PER_SITE", "300"))

    def scan_site(site):
        found, seen = [], set()
        for term in terms:
            for job in fetch_workday_jobs(site, max_jobs=cap, search_text=term):
                url = job.get("posting_url", "")
                if url in seen:
                    continue
                seen.add(url)
                host = urlparse(site).netloc.lower()
                known = COMPANY_NAMES.get(host)
                job["company_known"] = known
                job["company"] = known or pretty_company(job.get("company", ""))
                loc = job.get("location", "")
                if is_clearly_outside_india(loc) or not maybe_india(loc) or not not_senior(job.get("title", "")):
                    continue
                found.append(job)
        return found

    pending, done = [], 0
    with ThreadPoolExecutor(max_workers=int(os.environ.get("FETCH_WORKERS", "12"))) as pool:
        futures = {pool.submit(scan_site, s): s for s in sites}
        for fut in as_completed(futures):
            done += 1
            try:
                pending.extend(fut.result())
            except Exception as exc:
                errors.append(f"{futures[fut]}: {exc}")
            if done % 50 == 0:
                print(f"Fetched {done}/{len(sites)} sites; {len(pending)} India-candidate jobs; {elapsed()/60:.1f} min.")
            if elapsed() > fetch_budget:
                print("Fetch time budget reached; continuing with what was collected.")
                for f in futures:
                    f.cancel()
                break
    uniq = {}
    for j in pending:
        k = job_key(j.get("posting_url", ""))
        if k and k not in existing:
            uniq.setdefault(k, j)
    pending = list(uniq.values())
    # Internships first, so the LLM budget goes to the best matches.
    pending.sort(key=lambda j: 0 if ("intern" in j.get("title", "").lower() or "2027" in j.get("title", "")) else 1)
    print(f"{len(pending)} new India-candidate job(s) to evaluate ({elapsed()/60:.1f} min elapsed).")

    # Phase 2: enrich, confirm India from full details, then evaluate with the LLM.
    llm_calls = saved = 0
    stats = {"not_india": 0, "rule_reject": 0, "llm_reject": 0}
    batch = []
    deadline = start + budget

    def flush():
        nonlocal saved
        if batch:
            added, updated = save_jobs(spreadsheet, batch)
            saved += added + updated
            print(f"Sheet: wrote {added} new / {updated} updated row(s).")
            batch.clear()

    try:
        for job in pending:
            if time.time() > deadline or llm_calls >= max_llm:
                print(f"Stopping evaluation (elapsed {elapsed()/60:.0f} min, LLM calls {llm_calls}).")
                break
            try:
                job = enrich_workday_job(job)
                detail = (job.get("raw") or {}).get("detail") or {}
                extra = " ".join(map(str, detail.get("additionalLocations") or []))
                location = job.get("location", "")
                if not definitely_india(f"{location} {extra}"):
                    stats["not_india"] += 1
                    continue  # not verifiably in India
                if not rule_based_evaluate(job)["is_relevant"]:
                    stats["rule_reject"] += 1
                    continue  # wrong field or mandatory experience: no tokens spent
                llm_calls += 1
                result = evaluate_job(resume, job, deadline=deadline)   # waits as long as Groq says
                time.sleep(llm_delay)
                if not result.get("is_relevant", False):
                    stats["llm_reject"] += 1
                    continue
                batch.append({
                    "Company Name": job.get("company", ""),
                    "Job Title": job.get("title", ""),
                    "Location": location,
                    "Employment Type": result.get("employment_type") or job.get("employment_type", ""),
                    "Eligibility Concerns": "; ".join(map(str, result.get("eligibility_concerns", []))),
                    "Application URL": job.get("application_url") or job.get("posting_url") or "",
                    "Discovered At": now_ist(),
                    "Career Site URL": job.get("career_site", ""),
                    "Applied?": False,
                })
                print(f"Match: {job.get('company')} | {job.get('title')} | {location}")
                if len(batch) >= 5:
                    flush()
            except QuotaExhausted as exc:
                print(f"{exc} Saving progress; the rest will be evaluated on the next run.")
                break
            except Exception as exc:
                errors.append(f"{job.get('posting_url')}: {exc}")
                print(f"Failed for {job.get('posting_url')}: {exc}")
    finally:
        flush()   # never lose matches already found
    print(f"Outcome counts: {stats}")

    cleanup_expired_jobs(spreadsheet, max_age_days=3)
    print(f"Finished in {elapsed()/60:.1f} min. Saved {saved}; LLM calls {llm_calls}; errors {len(errors)}.")
    if errors:
        print("\n".join(errors[:20]))


if __name__ == "__main__":
    main()