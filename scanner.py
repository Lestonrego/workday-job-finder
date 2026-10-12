import csv
import hashlib
import json
import os
import random
import sys
import re
import time
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from dotenv import load_dotenv
from sheets import (get_spreadsheet, ensure_tabs, cleanup_expired_jobs, read_jobs, save_jobs,
                    compact_blank_rows, remove_duplicate_rows, job_key, load_seen, add_seen, purge_rows)
from workday import pretty_company, confirm_sites, discover_workday_sites, enrich_workday_job, fetch_india_jobs
from discovery import discover_workday_sites_from_search, COMPANY_NAMES, _career_root, _resolve_host, _host_of, _discover_github_listings
from matcher import evaluate_jobs_batch, now_ist, rule_based_evaluate, QuotaExhausted, required_years
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


# ----------------------------------------------------------------------------------- your company list
CSV_FILE = ROOT / "workday_companies.csv"
CSV_CACHE = ROOT / ".cache" / "csv_sites.json"
SHARDS = ["1", "3", "5", "12", "103", "501", "502", "503", "504", "108", "10", "2", "4"]
_SUFFIX = {"inc", "incorporated", "ltd", "limited", "llc", "llp", "corp", "corporation", "co", "company", "plc",
           "pvt", "private", "gmbh", "ag", "sa", "the", "and", "of"}


def site_key(url):
    root = _career_root(url) or url
    p = urlparse(root)
    return p.netloc.lower() + "/" + p.path.strip("/").split("/")[-1].lower()


def _tenant_guesses(name):
    words = [w for w in re.sub(r"\(.*?\)", " ", name.lower().replace("&", " ")).split()]
    words = [re.sub(r"[^a-z0-9]", "", w) for w in words]
    words = [w for w in words if w and w not in _SUFFIX]
    guesses = []
    if words:
        for g in ("".join(words), "".join(words[:2]), words[0]):
            if g and g not in guesses and len(g) > 2:
                guesses.append(g)
    return guesses


def _squash(name):
    words = [re.sub(r"[^a-z0-9]", "", w) for w in re.sub(r"\(.*?\)", " ", (name or "").lower().replace("&", " ")).split()]
    return "".join(w for w in words if w and w not in _SUFFIX)


def load_company_csv(known_sites=(), known_names=None):
    """Read workday_companies.csv (Company Name, Industry, and optionally career links).

    For each company the Workday site is found by, in order: a link in the file; a match against employers the
    scan already knows (by company name or by Workday tenant name); and finally probing the likely Workday
    addresses (company.wd5.myworkdayjobs.com ...). Probe results are cached between runs.
    Returns (career_site_urls, {host: company_name}, stats_text).
    """
    if not CSV_FILE.exists():
        return [], {}, "no workday_companies.csv in the repo (skipping)"
    text = CSV_FILE.read_bytes().decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4000], delimiters=",;\t|")
    except Exception:
        dialect = csv.excel
    rows = [r for r in csv.reader(text.splitlines(), dialect) if any((c or "").strip() for c in r)]
    if not rows:
        return [], {}, "workday_companies.csv is empty"
    has_url = lambda r: any("myworkdayjobs.com" in (c or "").lower() for c in r)
    header = None
    if not has_url(rows[0]):
        header = [c.strip().lower() for c in rows[0]]
        rows = rows[1:]
    name_col = None
    if header:
        for i, h in enumerate(header):
            if re.search(r"company|employer|organi[sz]ation|^name$|client|customer", h):
                name_col = i
                break
        if name_col is None:
            name_col = 0

    urls, names, nameless = [], {}, []
    for r in rows:
        cells = [(c or "").strip() for c in r]
        name = ""
        if name_col is not None and name_col < len(cells):
            name = cells[name_col]
        if not name or "myworkdayjobs.com" in name.lower():
            name = next((c for c in cells if c and "myworkdayjobs.com" not in c.lower() and not c.isdigit()
                         and not c.lower().startswith("http")), "")
        link_cells = [c for c in cells if "myworkdayjobs.com" in c.lower()]
        if link_cells:
            for cell in link_cells:
                for part in re.findall(r"(?:https?://)?[A-Za-z0-9.-]+\.myworkdayjobs\.com[^\s,;\"']*", cell):
                    url = part if part.startswith("http") else "https://" + part
                    root, host = _career_root(url), _host_of(url)
                    if not root and host:
                        root = _resolve_host(host)
                    if root:
                        urls.append(root)
                        if name:
                            names[urlparse(root).netloc.lower()] = name
        elif name:
            nameless.append(name)
    nameless = list(dict.fromkeys(nameless))

    # -- match against employers the scan already knows
    known_names = known_names or {}
    by_tenant, by_name = {}, {}
    for s in known_sites:
        host = urlparse(s).netloc.lower()
        by_tenant.setdefault(host.split(".")[0], []).append(s)
    for host, nm in known_names.items():
        by_name.setdefault(_squash(nm), set()).add(host)
    matched_known, unmatched = 0, []
    for nm in nameless:
        hit = []
        for g in _tenant_guesses(nm):
            hit += by_tenant.get(g, [])
        for host in by_name.get(_squash(nm), ()):
            hit += [s for s in known_sites if urlparse(s).netloc.lower() == host]
        if hit:
            matched_known += 1
            for s in dict.fromkeys(hit):
                urls.append(s)
                names[urlparse(s).netloc.lower()] = nm
        else:
            unmatched.append(nm)

    # -- probe likely Workday addresses for the rest (cached)
    try:
        probed = json.loads(CSV_CACHE.read_text(encoding="utf-8")).get("probed", {})
    except Exception:
        probed = {}
    todo = [n for n in unmatched if n not in probed][: int(os.environ.get("CSV_RESOLVE_MAX", "1500"))]
    if todo:
        from concurrent.futures import ThreadPoolExecutor as _TP
        tasks = [(n, g, s) for n in todo for g in _tenant_guesses(n)[:2] for s in SHARDS]
        print(f"workday_companies.csv: probing {len(tasks)} likely Workday addresses for {len(todo)} company name(s) (cached afterwards).", flush=True)

        def probe(task):
            nm, guess, shard = task
            return nm, _resolve_host(f"{guess}.wd{shard}.myworkdayjobs.com")

        found_by = {n: [] for n in todo}
        with _TP(max_workers=24) as pool:
            for nm, root in pool.map(probe, tasks):
                if root:
                    found_by[nm].append(root)
        for nm, roots in found_by.items():
            probed[nm] = list(dict.fromkeys(roots))
        try:
            CSV_CACHE.parent.mkdir(parents=True, exist_ok=True)
            CSV_CACHE.write_text(json.dumps({"probed": probed}), encoding="utf-8")
        except Exception:
            pass
    probed_hits = 0
    for nm in unmatched:
        for root in probed.get(nm, []):
            urls.append(root)
            names[urlparse(root).netloc.lower()] = nm
        probed_hits += 1 if probed.get(nm) else 0
    urls = list(dict.fromkeys(urls))
    stats = (f"{len(rows)} row(s); {len(nameless)} company name(s): {matched_known} matched employers already known, "
             f"{probed_hits} found by probing, {len(unmatched) - probed_hits} have no Workday site; {len(urls)} site(s) in total")
    return urls, names, stats


SITES_CACHE = ROOT / ".cache" / "sites.json"
SITES_CACHE_VERSION = 2      # bump to force a fresh, wider discovery


def load_sites_cache():
    """Confirmed sites from a previous run (refreshed every SITES_CACHE_DAYS days)."""
    try:
        data = json.loads(SITES_CACHE.read_text(encoding="utf-8"))
        max_age = float(os.environ.get("SITES_CACHE_DAYS", "7")) * 86400
        if data.get("version") == SITES_CACHE_VERSION and time.time() - data["saved_at"] < max_age and data["sites"]:
            return data["sites"]
    except Exception:
        pass
    return []


def save_sites_cache(sites):
    try:
        SITES_CACHE.parent.mkdir(parents=True, exist_ok=True)
        SITES_CACHE.write_text(json.dumps({"version": SITES_CACHE_VERSION, "saved_at": time.time(), "sites": sites}), encoding="utf-8")
    except Exception as exc:
        print(f"Could not save site cache: {exc}")


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
    budget = float(os.environ.get("RUN_BUDGET_MIN", "60")) * 60            # stay under the 6h Actions limit
    deadline = start + budget
    ai_deadline = deadline - 240                                            # last 10 min: save the unverified rest
    terms = [t.strip() for t in os.environ.get("SEARCH_TERMS", "intern,internship,trainee,graduate,fresher,entry level,early career,campus,new grad,apprentice,associate,junior,2027,2026").split(",") if t.strip()]
    batch_size = max(1, int(os.environ.get("LLM_BATCH", "8")))
    elapsed = lambda: (time.time() - start) / 60

    resume = read_resume_pdf()
    manual = [l.strip() for l in read_config_text("career_sites.txt").splitlines()
              if l.strip() and not l.strip().startswith("#")]
    sites = load_sites_cache()
    if sites:
        print(f"Using {len(sites)} cached Workday site(s); skipping discovery to save Actions minutes.")
    else:
        discovered = discover_workday_sites_from_search(max_sites=int(os.environ.get("MAX_DISCOVERED_SITES", "20000")))
        candidates = list(dict.fromkeys(manual + discovered))
        if not candidates:
            raise RuntimeError("Discovery returned no candidate sites.")
        sites = confirm_sites(candidates)
        print(f"{len(sites)} confirmed Workday site(s) of {len(candidates)} candidates ({elapsed():.1f} min).")
        if not sites:
            raise RuntimeError("No working public Workday career sites were found.")
        save_sites_cache(sites)
    # ---- Your own company list first (workday_companies.csv), then everything discovered automatically.
    if not COMPANY_NAMES:           # cached runs skip discovery, so reload company names (two small downloads)
        _discover_github_listings({}, set(), 0)
    csv_urls, csv_names, csv_stats = load_company_csv(sites, COMPANY_NAMES)
    print(f"workday_companies.csv: {csv_stats}")
    csv_sites = confirm_sites(csv_urls) if csv_urls else []
    if csv_urls:
        print(f"workday_companies.csv: {len(csv_sites)} of {len(csv_urls)} site(s) answer on Workday; they are scanned FIRST.")
    for host, name in csv_names.items():
        COMPANY_NAMES[host] = name          # your company names override the dataset names
    priority = {site_key(s) for s in csv_sites}

    # Sites you list in career_sites.txt are ALWAYS scanned (even with a cache).
    have = {urlparse(s).netloc.lower() + "/" + urlparse(s).path.strip("/").split("/")[-1].lower() for s in sites}
    extra = [m for m in manual if urlparse(m).netloc.lower() + "/" + urlparse(m).path.strip("/").split("/")[-1].lower() not in have]
    if extra:
        ok = confirm_sites(extra)
        print(f"career_sites.txt: {len(ok)} of {len(extra)} extra site(s) added and working.")
        sites = sites + ok
    rest = [s for s in dict.fromkeys(sites) if site_key(s) not in priority]
    random.shuffle(rest)
    sites = list(dict.fromkeys(csv_sites)) + rest        # csv companies first, then the normal discovery
    print(f"Scanning {len(csv_sites)} company-list site(s) first, then {len(rest)} discovered site(s).")

    spreadsheet = get_spreadsheet()
    ensure_tabs(spreadsheet)
    cleanup_expired_jobs(spreadsheet, max_age_days=3)
    remove_duplicate_rows(spreadsheet)

    def low_quality(row):
        note = (row.get("Eligibility Concerns") or "").lower()
        if "ai review not completed" in note or "keyword match only" in note:
            return not rule_based_evaluate({"title": row.get("Job Title", ""), "description": ""})["is_relevant"]
        return bool(re.search(r"does not meet|not meet|\d+\s*[-\u2013+]\s*\d*\s*years|\d+\+?\s*years|lacks|missing required", note))

    purge_rows(spreadsheet, low_quality)          # clean up junk written by earlier runs
    compact_blank_rows(spreadsheet)
    _, sheet_rows, _ = read_jobs(spreadsheet)
    known = {job_key(r.get("Application URL", "")) for r in sheet_rows}
    seen_keys = load_seen(spreadsheet)
    known |= seen_keys
    print(f"{len(known)} job(s) already known (sheet rows + Seen tab) will be skipped.")
    errors, seen_new = [], []
    debug = os.environ.get("DEBUG_SITE", "").strip().lower()      # e.g. DEBUG_SITE=thermofisher
    samples = {}

    def note(reason, job, extra=""):
        bucket = samples.setdefault(reason, [0, []])
        bucket[0] += 1
        if len(bucket[1]) < 12:
            bucket[1].append(f"{job.get('company', '')} | {job.get('title', '')} {extra}".strip())

    # ---- Phase 1: only India listings (server-side India filter), intern/graduate/trainee/2027 searches.
    cap = min(int(os.environ.get("MAX_JOBS_PER_SITE", "400")), 400)

    def scan_site(site):
        host = urlparse(site).netloc.lower()
        out = []
        listed = fetch_india_jobs(site, terms, max_jobs=cap)
        if debug and debug in site.lower():
            print(f"[debug] {site}: {len(listed)} India listing(s) after searching {len(terms)} terms")
            for j in listed[:60]:
                print(f"[debug]   listing: {j.get('title')} | {j.get('location')}")
        for job in listed:
            known_name = COMPANY_NAMES.get(host)
            job["priority"] = site_key(site) in priority
            job["company_known"] = known_name
            job["company"] = known_name or pretty_company(job.get("company", ""))
            loc = job.get("location", "")
            if not job.get("india_filtered"):
                if is_clearly_outside_india(loc) or not maybe_india(loc):
                    continue
            if not not_senior(job.get("title", "")):
                note("senior title (at listing)", job)
                continue
            out.append(job)
        return out

    pending, done = [], 0
    fetch_started = time.time()
    fetch_max = float(os.environ.get("FETCH_MAX_MIN", "18")) * 60
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
            if time.time() - fetch_started > fetch_max:
                print("Fetch time budget reached; continuing with what was collected.")
                for f in futures:
                    f.cancel()
                break
    uniq = {}
    for j in pending:
        k = job_key(j.get("posting_url", ""))
        if k and k not in known:
            uniq.setdefault(k, j)
    print(f"{len(uniq)} new of {len({job_key(j.get('posting_url', '')) for j in pending})} India jobs (rest already judged).")
    pending = sorted(uniq.items(), key=lambda kv: (0 if kv[1].get("priority") else 1, 0 if ("intern" in kv[1].get("title", "").lower() or "2027" in kv[1].get("title", "")) else 1))
    print(f"{len(pending)} new India job(s) to check ({elapsed():.1f} min elapsed).")

    # ---- Phase 2: details in parallel, India + keyword/experience rules (free).
    def prepare(item):
        try:
            return _prepare(item)
        except Exception as exc:          # one bad posting must never stop the run
            errors.append(f"{item[1].get('posting_url')}: {exc}")
            return item[0], item[1], "error"

    def _prepare(item):
        key, job = item
        job = enrich_workday_job(job)
        detail = (job.get("raw") or {}).get("detail") or {}
        extra = " ".join(map(str, detail.get("additionalLocations") or []))
        if not job.get("india_filtered") and not definitely_india(f"{job.get('location', '')} {extra}"):
            note("location not verifiably India", job, f"[{job.get('location', '')}]")
            return key, job, "not_india"
        job["location"] = pick_india_location(job)
        verdict = rule_based_evaluate(job)
        if debug and debug in (job.get("career_site", "") or "").lower():
            print(f"[debug] {job.get('title')} | {job.get('location')} -> {'PASS' if verdict['is_relevant'] else 'dropped: ' + verdict['why']}")
        if not verdict["is_relevant"]:
            note("filtered: " + verdict["why"], job)
            return key, job, "rule_reject"
        return key, job, "ok"

    stats = {"not_india": 0, "rule_reject": 0, "ai_reject": 0, "error": 0}
    shortlist = []
    with ThreadPoolExecutor(max_workers=int(os.environ.get("DETAIL_WORKERS", "12"))) as pool:
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
            if n % 500 == 0:
                print(f"Checked details {n}/{len(pending)}; shortlist {len(shortlist)}; {elapsed():.1f} min.")
            if time.time() > deadline - 420:
                print("Out of time during detail checks.")
                break
    print(f"Shortlist for AI review: {len(shortlist)} job(s). Dropped by free checks: {stats}.")

    # ---- Phase 3: Groq reviews 8 jobs per call; waits as Groq asks; leftovers saved as 'unverified'.
    saved = 0

    def to_record(job, result, unverified=False):
        concerns = result.get("eligibility_concerns", []) if result else []
        note = "; ".join(map(str, concerns))
        if unverified:
            note = "Keyword match only (AI review not completed); verify requirements"
        return {
            "Company Name": (job.get("company") if job.get("company_verified") else "")
                            or ((result or {}).get("company_name") or "").strip() or job.get("company", ""),
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
                elif required_years(job.get("description", "")) >= 1 and "intern" not in job.get("title", "").lower():
                    stats["ai_reject"] += 1          # JD demands experience; never trust the AI over that
                    note("JD requires experience (override)", job)
                elif result.get("is_relevant") and float(result.get("match_score") or 0) >= float(os.environ.get("MIN_MATCH_SCORE", "50")):
                    records.append(to_record(job, result))
                    print(f"Match: {job.get('company')} | {job.get('title')} | {job.get('location')}")
                else:
                    stats["ai_reject"] += 1
                    note("AI rejected", job, f"-> {str(result.get('reasoning', ''))[:110]} (score {result.get('match_score', '')})")
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
            cap_unverified = int(os.environ.get("MAX_UNVERIFIED", "40"))
            take = leftovers[:cap_unverified]          # internships / 2027 roles come first
            write([to_record(j, None, unverified=True) for _, j in take], [k for k, _ in take])
            print(f"Saved {len(take)} job(s) without AI review; {len(leftovers) - len(take)} left for the next run.")
        add_seen(spreadsheet, seen_new)

    cleanup_expired_jobs(spreadsheet, max_age_days=3)
    print("---- Why jobs were dropped (count, then examples) ----")
    for reason, (count, examples) in sorted(samples.items(), key=lambda kv: -kv[1][0]):
        print(f"[{count}] {reason}")
        for ex in examples:
            print(f"      {ex}")
    print(f"Finished in {elapsed():.1f} min. New rows: {saved}; AI rejected: {stats['ai_reject']}; "
          f"dropped: not India {stats['not_india']}, rules {stats['rule_reject']}; errors: {len(errors)}.")
    if errors:
        print("\n".join(errors[:20]))


if __name__ == "__main__":
    main()