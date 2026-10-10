import os
import re
from pathlib import Path
from dotenv import load_dotenv
from sheets import get_spreadsheet, ensure_tabs, upsert_job, cleanup_expired_jobs
from workday import discover_workday_sites, enrich_workday_job, fetch_workday_jobs
from discovery import discover_workday_sites_from_search
from matcher import evaluate_job, now_ist
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

INDIA_HINTS = [
    "india", "bengaluru", "bangalore", "hyderabad", "pune", "chennai", "mumbai", "navi mumbai",
    "gurgaon", "gurugram", "noida", "delhi", "kolkata", "ahmedabad", "mangalore", "mysore",
    "kochi", "coimbatore", "jaipur", "chandigarh", "indore", "trivandrum", "thiruvananthapuram",
    "nagpur", "bhubaneswar", "vadodara", "visakhapatnam", "lucknow", "karnataka", "maharashtra",
    "telangana", "tamil nadu", "haryana", "uttar pradesh", "kerala", "gujarat", "west bengal",
]
ENTRY_HINTS = [
    "intern", "trainee", "graduate", "fresher", "entry", "junior", "apprentice", "campus",
    "early career", "early talent", "university", "student", "associate", "analyst", "engineer i",
    "engineer 1", "developer i", "new grad", "co-op", "coop", "rotational",
]
SENIOR_HINTS = ["senior", "sr.", "sr ", "staff", "principal", "lead", "manager", "director", "head of", "vp ", "architect"]


def maybe_india(location):
    t = (location or "").lower().strip()
    if not t or "remote" in t or re.search(r"\d+\s+locations?", t):
        return True
    return any(h in t for h in INDIA_HINTS)


def maybe_entry_level(title):
    t = (title or "").lower()
    if any(h in t for h in SENIOR_HINTS) and "intern" not in t:
        return False
    return any(h in t for h in ENTRY_HINTS)


def main():
    # Store editable, non-secret configuration in repository files.
    resume = read_resume_pdf()
    sites = [
        line.strip() for line in read_config_text("career_sites.txt").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    # Automatically search public web indexes for Workday employers. Manual seed URLs
    # remain optional and are merged with search-discovered candidates.
    max_discovered = int(os.environ.get("MAX_DISCOVERED_SITES", "5000"))
    discovered = discover_workday_sites_from_search(max_sites=max_discovered)
    seed_sites = list(dict.fromkeys(sites + discovered))
    if not seed_sites:
        raise RuntimeError(
            "Automatic discovery and fallback seeds returned no candidates. "
            "Check public search access in the Actions logs and retry the workflow."
        )

    # Confirm candidates against Workday's public jobs API and probe related career paths.
    sites = discover_workday_sites(seed_sites)
    if not sites:
        raise RuntimeError("No working public Workday career sites were found. Check career_sites.txt.")
    print(f"Scanning {len(sites)} confirmed Workday career site(s).")

    spreadsheet = get_spreadsheet()
    ensure_tabs(spreadsheet)
    cleanup_expired_jobs(spreadsheet, max_age_days=3)
    errors = []
    total = 0

    for site in sites:
        try:
            jobs = fetch_workday_jobs(site, max_jobs=int(os.environ.get("MAX_JOBS_PER_SITE", "100000")))
            print(f"{site}: collected {len(jobs)} listings")
            for job in jobs:
                total += 1
                location = job.get("location", "")
                if is_clearly_outside_india(location):
                    print(f"Skipping clearly non-India location: {job.get('title')} | {location}")
                    continue
                if not maybe_india(location) or not maybe_entry_level(job.get("title", "")):
                    continue
                try:
                    # Enrich India-like and ambiguous city/state-only listings. The LLM decides
                    # whether the final location is in India and whether experience is required.
                    job = enrich_workday_job(job)
                    location = job.get("location", location)
                    result = evaluate_job(resume, job)
                    # No rigid match-score threshold. Keep plausible opportunities for review.
                    if not result.get("is_relevant", True):
                        continue
                    url = job.get("posting_url") or job.get("application_url") or ""
                    record = {
                        "Company Name": job.get("company", ""),
                        "Job Title": job.get("title", ""),
                        "Location": location,
                        "Employment Type": result.get("employment_type", job.get("employment_type", "")),
                        "Eligibility Concerns": "; ".join(map(str, result.get("eligibility_concerns", []))),
                        "Application URL": job.get("application_url") or job.get("posting_url") or "",
                        "Discovered At": now_ist(),
                        "Career Site URL": job.get("career_site", ""),
                        "Applied?": False,
                    }
                    action = upsert_job(spreadsheet, record)
                    print(f"{action}: {record['Job Title']} | {location} | {record['Employment Type']}")
                except Exception as exc:
                    print(f"Evaluation failed for {job.get('posting_url')}: {exc}")
                    errors.append(f"{job.get('posting_url')}: {exc}")
        except Exception as exc:
            print(f"Site scan failed: {site}: {exc}")
            errors.append(f"{site}: {exc}")

    # Run expiry cleanup again after upserts. Existing jobs retain their first-seen timestamp,
    # so updating a duplicate never extends its 3-day lifetime.
    cleanup_expired_jobs(spreadsheet, max_age_days=3)
    print(f"Finished. Processed {total} listings; errors: {len(errors)}")
    if errors:
        print("\n".join(errors[:30]))
    if total == 0 and errors:
        raise RuntimeError("The scan could not process any listings. Check the logs and career site URLs.")

if __name__ == "__main__":
    main()
