"""Standalone hourly cleanup for job rows older than three days."""
from dotenv import load_dotenv
from sheets import get_spreadsheet, ensure_tabs, cleanup_expired_jobs

load_dotenv()

if __name__ == "__main__":
    spreadsheet = get_spreadsheet()
    ensure_tabs(spreadsheet)
    cleanup_expired_jobs(spreadsheet, max_age_days=3)
