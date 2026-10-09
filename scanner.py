import os
from pathlib import Path
from dotenv import load_dotenv
from sheets import get_spreadsheet, ensure_tabs, upsert_job
from workday import fetch_workday_jobs
from matcher import evaluate_job, now_ist

load_dotenv()
ROOT = Path(__file__).resolve().parent

def read_config_text(filename):
    path = ROOT / filename
    if not path.exists():
        raise RuntimeError(f"Missing {filename}. Add it to the GitHub repository.")
    return path.read_text(encoding="utf-8").strip()

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
    resume = read_config_text("resume.txt")
    sites = [
        line.strip() for line in read_config_text("career_sites.txt").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    if not sites:
        raise RuntimeError("career_sites.txt must contain at least one employer Workday career URL.")

    spreadsheet = get_spreadsheet()
    ensure_tabs(spreadsheet)
    errors = []
    total = 0

    for site in sites:
        try:
            jobs = fetch_workday_jobs(site, max_jobs=int(os.environ.get("MAX_JOBS_PER_SITE", "500")))
            print(f"{site}: collected {len(jobs)} listings")
            for job in jobs:
                total += 1
                location = job.get("location", "")
                if not is_india_location(location):
                    print(f"Skipping non-India/unknown location: {job.get('title')} | {location}")
                    continue
                try:
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
                    print(f"{action}: {record['Job Title']} | {location} | {record['Match Score']}")
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
