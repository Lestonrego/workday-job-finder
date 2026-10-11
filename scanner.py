import os
import sys
import re
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from sheets import (get_spreadsheet, ensure_tabs, cleanup_expired_jobs, read_jobs, save_jobs,
                    compact_blank_rows, remove_duplicate_rows, job_key, load_seen, add_seen)
from workday import pretty_company, confirm_sites, discover_workday_sites, enrich_workday_job, fetch_india_jobs
from discovery import discover_workday_sites_from_search, COMPANY_NAMES
from matcher import evaluate_jobs_batch, now_ist, rule_based_evaluate, QuotaExhausted
from pypdf import PdfReader

load_dotenv()
# Show output live in GitHub Actions (piped stdout is otherwise block-buffered and looks frozen).
try:
    sys.stdout.reconfigure(line_buffering=True)
except Exception:
    pass
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


def pick_india_location(job):
    """Prefer an Indian location string for the sheet when a posting lists several locations."""
    detail = (job.get("raw") or {}).get("detail") or {}
    candidates = [job.get("location", "")] + [str(x) for x in (detail.get("additionalLocations") or [])]
    for c in candidates:
        if definitely_india(c):
            return c
    return job.get("location", "")


def main():
    start = time.time()
    budget = float(os.environ.get("RUN_BUDGET_MIN", "270")) * 60            # stay under the 6h Actions limit
    deadline = start + budget
    ai_deadline = deadline - 600                                            # last 10 min: save the unverified rest
    terms = [t.strip() for t in os.environ.get("SEARCH_TERMS", "intern,graduate,trainee,2027").split(",") if t.strip()]
    batch_size = max(1, int(os.environ.get("LLM_BATCH", "8")))
    elapsed = lambda: (time.time() - start) / 60

    resume = read_resume_pdf()
    manual = [l.strip() for l in read_config_text("career_sites.txt").splitlines()
              if l.strip() and not l.strip().startswith("#")]
    discovered = discover_workday_sites_from_search(max_sites=int(os.environ.get("MAX_DISCOVERED_SITES", "5000")))
    candidates = list(dict.fromkeys(manual + discovered))
    if not candidates:
        raise RuntimeError("Discovery returned no candidate sites.")
    sites = confirm_sites(candidates)
    print(f"{len(sites)} confirmed Workday site(s) of {len(candidates)} candidates ({elapsed():.1f} min).")
    if not sites:
        raise RuntimeError("No working public Workday career sites were found.")

    spreadsheet = get_spreadsheet()
    ensure_tabs(spreadsheet)
    cleanup_expired_jobs(spreadsheet, max_age_days=3)
    remove_duplicate_rows(spreadsheet)
    compact_blank_rows(spreadsheet)
    _, sheet_rows, _ = read_jobs(spreadsheet)
    known = {job_key(r.get("Application URL", "")) for r in sheet_rows}
    known |= load_seen(spreadsheet)
    errors, seen_new = [], []

    # ---- Phase 1: only India listings (server-side India filter), intern/graduate/trainee/2027 searches.
    cap = min(int(os.environ.get("MAX_JOBS_PER_SITE", "300")), 400)

    def scan_site(site):
        host = urlparse(site).netloc.lower()
        out = []
        for job in fetch_india_jobs(site, terms, max_jobs=cap):
            known_name = COMPANY_NAMES.get(host)
            job["company_known"] = known_name
            job["company"] = known_name or pretty_company(job.get("company", ""))
            loc = job.get("location", "")
            if not job.get("india_filtered"):
                if is_clearly_outside_india(loc) or not maybe_india(loc):
                    continue
            if not not_senior(job.get("title", "")):
                continue
            out.append(job)
        return out

    pending, done = [], 0
    with ThreadPoolExecutor(max_workers=int(os.environ.get("FETCH_WORKERS", "12"))) as pool:
        futures = {pool.submit(scan_site, s): s for s in sites}
        for fut in as_completed(futures):
            done += 1
            try:
                pending.extend(fut.result())
            except Exception as exc:
                errors.append(f"{futures[fut]}: {exc}")
            if done % 100 == 0:
                print(f"Fetched {done}/{len(sites)} sites; {len(pending)} India jobs; {elapsed():.1f} min.")
            if time.time() - start > budget * 0.4:
                print("Fetch time budget reached; continuing with what was collected.")
                for f in futures:
                    f.cancel()
                break
    uniq = {}
    for j in pending:
        k = job_key(j.get("posting_url", ""))
        if k and k not in known:
            uniq.setdefault(k, j)
    pending = sorted(uniq.items(), key=lambda kv: 0 if ("intern" in kv[1].get("title", "").lower() or "2027" in kv[1].get("title", "")) else 1)
    print(f"{len(pending)} new India job(s) to check ({elapsed():.1f} min elapsed).")

    # ---- Phase 2: details in parallel, India + keyword/experience rules (free).
    def prepare(item):
        key, job = item
        job = enrich_workday_job(job)
        detail = (job.get("raw") or {}).get("detail") or {}
        extra = " ".join(map(str, detail.get("additionalLocations") or []))
        if not definitely_india(f"{job.get('location', '')} {extra}"):
            return key, job, "not_india"
        job["location"] = pick_india_location(job)
        if not rule_based_evaluate(job)["is_relevant"]:
            return key, job, "rule_reject"
        return key, job, "ok"

    stats = {"not_india": 0, "rule_reject": 0, "ai_reject": 0}
    shortlist = []
    with ThreadPoolExecutor(max_workers=int(os.environ.get("DETAIL_WORKERS", "8"))) as pool:
        for n, outcome in enumerate(pool.map(prepare, pending), start=1):
            try:
                key, job, verdict = outcome
            except Exception as exc:
                errors.append(str(exc))
                continue
            if verdict == "ok":
                shortlist.append((key, job))
            else:
                stats[verdict] += 1
                seen_new.append(key)
            if n % 500 == 0:
                print(f"Checked details {n}/{len(pending)}; shortlist {len(shortlist)}; {elapsed():.1f} min.")
            if time.time() > deadline - 900:
                print("Out of time during detail checks.")
                break
    print(f"Shortlist for AI review: {len(shortlist)} job(s). Dropped: {stats}.")

    # ---- Phase 3: Groq reviews 8 jobs per call; waits as Groq asks; leftovers saved as 'unverified'.
    saved = 0

    def to_record(job, result, unverified=False):
        concerns = result.get("eligibility_concerns", []) if result else []
        note = "; ".join(map(str, concerns))
        if unverified:
            note = "AI review not completed - keyword match only, check requirements manually"
        return {
            "Company Name": job.get("company", ""),
            "Job Title": job.get("title", ""),
            "Location": job.get("location", ""),
            "Employment Type": (result or {}).get("employment_type") or job.get("employment_type") or "Internship/Entry-level",
            "Eligibility Concerns": note,
            "Application URL": job.get("application_url") or job.get("posting_url") or "",
            "Discovered At": now_ist(),
            "Career Site URL": job.get("career_site", ""),
            "Applied?": False,
        }

    def write(records, keys):
        nonlocal saved
        if records:
            added, updated = save_jobs(spreadsheet, records)
            saved += added
            print(f"Sheet: wrote {added} new / {updated} updated row(s).", flush=True)
        add_seen(spreadsheet, keys)

    index = 0
    ai_ok = True
    try:
        while index < len(shortlist) and ai_ok and time.time() < ai_deadline:
            chunk = shortlist[index:index + batch_size]
            try:
                results = evaluate_jobs_batch(resume, [j for _, j in chunk], deadline=ai_deadline)
            except QuotaExhausted as exc:
                print(f"{exc} Saving the rest as unverified.")
                ai_ok = False
                break
            index += len(chunk)
            records, keys = [], []
            for (key, job), result in zip(chunk, results):
                if result is None:                      # model skipped it: keep for manual review
                    records.append(to_record(job, None, unverified=True))
                elif result.get("is_relevant"):
                    records.append(to_record(job, result))
                    print(f"Match: {job.get('company')} | {job.get('title')} | {job.get('location')}")
                else:
                    stats["ai_reject"] += 1
                keys.append(key)
            write(records, keys)
            time.sleep(float(os.environ.get("LLM_DELAY_SEC", "1")))
    except Exception as exc:
        errors.append(f"AI review stopped: {exc}")
        print(f"AI review stopped: {exc}")
    finally:
        # Anything the AI did not get to is still saved so you can check it manually.
        leftovers = shortlist[index:]
        if leftovers:
            write([to_record(j, None, unverified=True) for _, j in leftovers], [k for k, _ in leftovers])
            print(f"Saved {len(leftovers)} job(s) without AI review.")
        add_seen(spreadsheet, seen_new)

    cleanup_expired_jobs(spreadsheet, max_age_days=3)
    print(f"Finished in {elapsed():.1f} min. New rows: {saved}; AI rejected: {stats['ai_reject']}; "
          f"dropped: not India {stats['not_india']}, rules {stats['rule_reject']}; errors: {len(errors)}.")
    if errors:
        print("\n".join(errors[:20]))


if __name__ == "__main__":
    main()