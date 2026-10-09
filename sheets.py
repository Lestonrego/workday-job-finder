import json
import os
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
        # This project is configured for the user's empty tracker and these exact columns.
        # Reset only when the header row differs, so repeated scans do not erase job rows.
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
                    "endColumnIndex": applied_col
                },
                "rule": {
                    "condition": {"type": "BOOLEAN"},
                    "strict": True,
                    "showCustomUi": True
                }
            }
        }]
    })

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
        for old_record in existing_rows:
            old_key = (old_record.get("Application URL") or "").strip().rstrip("/")
            if old_key == key:
                existing_applied = str(old_record.get("Applied?", "")).strip().upper() == "TRUE"
                break

    record["Applied?"] = existing_applied
    row = [record.get(header, "") for header in JOB_HEADERS]

    if key and key in url_to_row:
        row_number = url_to_row[key]
        ws.update(range_name=f"A{row_number}:I{row_number}", values=[row], value_input_option="USER_ENTERED")
        return "updated"

    ws.append_row(row, value_input_option="USER_ENTERED")
    return "added"
