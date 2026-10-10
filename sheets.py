import json
import os
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

JOB_HEADERS = [
    "Company Name",
    "Job Title",
    "Location",
    "Employment Type",
    "Eligibility Concerns",
    "Application URL",
    "Discovered At",
    "Career Site URL",
    "Applied?",
]
SETTINGS_HEADERS = ["key", "value"]
IST = ZoneInfo("Asia/Kolkata")


def get_spreadsheet():
    raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
    sheet_id = os.environ.get("GOOGLE_SHEET_ID", "").strip()
    if not raw or not sheet_id:
        raise RuntimeError("Set GOOGLE_SERVICE_ACCOUNT_JSON and GOOGLE_SHEET_ID.")
    info = json.loads(raw)
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_info(info, scopes=scopes)
    client = gspread.authorize(creds)
    return client.open_by_key(sheet_id)


def ensure_tabs(spreadsheet):
    names = {ws.title for ws in spreadsheet.worksheets()}
    if "Jobs" not in names:
        ws = spreadsheet.add_worksheet(title="Jobs", rows=1000, cols=len(JOB_HEADERS))
    else:
        ws = spreadsheet.worksheet("Jobs")

    current_headers = ws.row_values(1)
    if current_headers != JOB_HEADERS:
        # Only reset when the schema differs. Normal scans never clear job rows.
        ws.clear()
        ws.update(range_name="A1:I1", values=[JOB_HEADERS], value_input_option="RAW")

    applied_col = JOB_HEADERS.index("Applied?") + 1
    spreadsheet.batch_update({
        "requests": [{
            "setDataValidation": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": 1,
                    "endRowIndex": 5000,
                    "startColumnIndex": applied_col - 1,
                    "endColumnIndex": applied_col,
                },
                "rule": {
                    "condition": {"type": "BOOLEAN"},
                    "strict": True,
                    "showCustomUi": True,
                },
            }
        }]
    })
    return ws


def _parse_discovered_at(value):
    value = (value or "").strip()
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M:%S", "%m/%d/%Y %H:%M:%S"):
        try:
            parsed = datetime.strptime(value, fmt)
            return parsed.replace(tzinfo=IST)
        except ValueError:
            pass
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=IST) if parsed.tzinfo is None else parsed.astimezone(IST)
    except ValueError:
        return None


def cleanup_expired_jobs(spreadsheet, max_age_days=3):
    """Delete Jobs-sheet rows at least max_age_days old, based on first discovery time.

    Rows with missing/unparseable timestamps are preserved instead of risking deletion.
    Delete requests are sent in one batch and processed from the bottom up.
    """
    ws = spreadsheet.worksheet("Jobs")
    values = ws.get_all_values()
    if len(values) <= 1:
        print("Expiry cleanup: no job rows to check.")
        return 0
    headers = values[0]
    try:
        date_index = headers.index("Discovered At")
    except ValueError:
        print("Expiry cleanup skipped: 'Discovered At' column is missing.")
        return 0

    cutoff = datetime.now(IST) - timedelta(days=max_age_days)
    expired_rows = []
    for sheet_row_number, row in enumerate(values[1:], start=2):
        timestamp = row[date_index] if len(row) > date_index else ""
        discovered_at = _parse_discovered_at(timestamp)
        if discovered_at is not None and discovered_at <= cutoff:
            expired_rows.append(sheet_row_number)

    if not expired_rows:
        print(f"Expiry cleanup: no rows older than {max_age_days} days.")
        return 0

    # Sheets API uses zero-based, end-exclusive row indexes. Descending order avoids shifts.
    requests = []
    for row_number in sorted(expired_rows, reverse=True):
        requests.append({
            "deleteDimension": {
                "range": {
                    "sheetId": ws.id,
                    "dimension": "ROWS",
                    "startIndex": row_number - 1,
                    "endIndex": row_number,
                }
            }
        })
    spreadsheet.batch_update({"requests": requests})
    print(f"Expiry cleanup: deleted {len(expired_rows)} job row(s) older than {max_age_days} days.")
    return len(expired_rows)


def read_jobs(spreadsheet):
    ws = spreadsheet.worksheet("Jobs")
    values = ws.get_all_values()
    if len(values) <= 1:
        return ws, [], {}
    headers = values[0]
    rows = []
    url_to_row = {}
    for row_number, values_row in enumerate(values[1:], start=2):
        padded = values_row + [""] * (len(headers) - len(values_row))
        record = dict(zip(headers, padded))
        rows.append(record)
        key = (record.get("Application URL") or "").strip().rstrip("/")
        if key:
            url_to_row[key] = row_number
    return ws, rows, url_to_row


def upsert_job(spreadsheet, record):
    ws, existing_rows, url_to_row = read_jobs(spreadsheet)
    key = (record.get("Application URL") or "").strip().rstrip("/")
    existing_applied = False

    if key and key in url_to_row:
        row_number = url_to_row[key]
        for old_record in existing_rows:
            old_key = (old_record.get("Application URL") or "").strip().rstrip("/")
            if old_key == key:
                existing_applied = str(old_record.get("Applied?", "")).strip().upper() == "TRUE"
                # Keep the original timestamp: repeated scans must not extend the 3-day lifetime.
                record["Discovered At"] = old_record.get("Discovered At") or record.get("Discovered At", "")
                break
        record["Applied?"] = existing_applied
        row = [record.get(header, "") for header in JOB_HEADERS]
        ws.update(range_name=f"A{row_number}:I{row_number}", values=[row], value_input_option="USER_ENTERED")
        return "updated"

    record["Applied?"] = False
    row = [record.get(header, "") for header in JOB_HEADERS]
    ws.append_row(row, value_input_option="USER_ENTERED")
    return "added"
