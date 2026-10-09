import os
from pathlib import Path
from dotenv import load_dotenv
from sheets import get_spreadsheet, ensure_tabs, upsert_job
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

def is_india_location(location):
    text = (location or "").lower()
    india_markers = [
        "india", "bengaluru", "bangalore", "mangaluru", "mangalore", "hyderabad",
        "chennai", "pune", "mumbai", "new delhi", "delhi", "gurugram", "gurgaon",
        "noida", "kolkata", "kochi", "cochin", "coimbatore", "ahmedabad", "jaipur",
        "indore", "bhubaneswar", "thiruvananthapuram", "trivandrum", "mysuru", "mysore",
        "nagpur", "lucknow", "chandigarh", "visakhapatnam", "vizag", "vadodara",
        "surat", "tiruchirappalli", "trichy", "remote - india", "india remote"
    ]
    return any(marker in text for marker in india_markers)

def main():
    # Store editable, non-secret configuration in repository files.
    resume = read_resume_pdf()
    sites = [
        line.strip() for line in read_config_text("career_sites.txt").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]

    # Automatically search public web indexes for Workday employers. Manual seed URLs
    # remain optional and are merged with search-discovered candidates.
    max_discovered = int(os.environ.get("MAX_DISCOVERED_SITES", "30"))
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
    errors = []
    total = 0

    for site in sites:
        try:
            jobs = fetch_workday_jobs(site, max_jobs=int(os.environ.get("MAX_JOBS_PER_SITE", "10000")))
            print(f"{site}: collected {len(jobs)} listings")
            for job in jobs:
                total += 1
                location = job.get("location", "")
                if not is_india_location(location):
                    print(f"Skipping non-India/unknown location: {job.get('title')} | {location}")
                    continue
                try:
                    # Full descriptions are fetched only for jobs whose listing location looks Indian.
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

    print(f"Finished. Processed {total} listings; errors: {len(errors)}")
    if errors:
        print("\n".join(errors[:30]))
    if total == 0 and errors:
        raise RuntimeError("The scan could not process any listings. Check the logs and career site URLs.")

if __name__ == "__main__":
    main()
