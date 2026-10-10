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

    if ws.row_count < 2000:
        ws.resize(rows=2000)
    applied_col = JOB_HEADERS.index("Applied?") + 1
    spreadsheet.batch_update({
        "requests": [{
            "setDataValidation": {
                "range": {
                    "sheetId": ws.id,
                    "startRowIndex": 1,
                    "endRowIndex": 2000,
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


def _norm_url(value):
    return (value or "").strip().rstrip("/")


def job_key(url):
    """Identity of a posting: host + the part after /job/ (ignores locale, site, query, case)."""
    from urllib.parse import urlparse
    url = _norm_url(url)
    if not url:
        return ""
    parsed = urlparse(url)
    path = parsed.path
    tail = path.split("/job/", 1)[1] if "/job/" in path else path
    return (parsed.netloc.lower() + "/job/" + tail.strip("/").lower()) if parsed.netloc else url.lower()


def content_key(record_or_row, headers=None):
    """Same company + title + location means the same job even if it was re-posted under a new URL."""
    get = record_or_row.get if isinstance(record_or_row, dict) else (lambda h: record_or_row[(headers or JOB_HEADERS).index(h)] if len(record_or_row) > (headers or JOB_HEADERS).index(h) else "")
    parts = [" ".join((get(h) or "").lower().split()) for h in ("Company Name", "Job Title", "Location")]
    return "|".join(parts) if all(parts) else ""


def _is_blank(row):
    """A row is blank when every data column (everything except the Applied? checkbox) is empty."""
    return not any((cell or "").strip() for cell in row[: len(JOB_HEADERS) - 1])


def compact_blank_rows(spreadsheet):
    """Delete empty rows sitting between data rows so the table has no gaps."""
    ws = spreadsheet.worksheet("Jobs")
    values = ws.get_all_values()
    last = 0
    for number, row in enumerate(values[1:], start=2):
        if not _is_blank(row):
            last = number
    gaps = [n for n, row in enumerate(values[1:], start=2) if n < last and _is_blank(row)]
    if not gaps:
        return 0
    requests = [{
        "deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS",
                                      "startIndex": n - 1, "endIndex": n}}
    } for n in sorted(gaps, reverse=True)]
    spreadsheet.batch_update({"requests": requests})
    print(f"Removed {len(gaps)} blank row(s) between job rows.")
    return len(gaps)


def remove_duplicate_rows(spreadsheet):
    """Delete duplicate job rows already in the sheet (keeps an Applied row, else the first)."""
    ws = spreadsheet.worksheet("Jobs")
    values = ws.get_all_values()
    width = len(JOB_HEADERS)
    url_col = JOB_HEADERS.index("Application URL")
    applied_col = JOB_HEADERS.index("Applied?")
    keeper, delete = {}, []
    for number, row in enumerate(values[1:], start=2):
        row = row + [""] * (width - len(row))
        if _is_blank(row):
            continue
        keys = [k for k in (job_key(row[url_col]), content_key(row)) if k]
        match = next((keeper[k] for k in keys if k in keeper), None)
        if match is None:
            for k in keys:
                keeper[k] = (number, str(row[applied_col]).strip().upper() == "TRUE")
            continue
        kept_number, kept_applied = match
        if str(row[applied_col]).strip().upper() == "TRUE" and not kept_applied:
            delete.append(kept_number)       # keep the one you marked applied
            for k in keys:
                keeper[k] = (number, True)
        else:
            delete.append(number)
    delete = sorted(set(delete), reverse=True)
    if not delete:
        return 0
    spreadsheet.batch_update({"requests": [{
        "deleteDimension": {"range": {"sheetId": ws.id, "dimension": "ROWS",
                                      "startIndex": n - 1, "endIndex": n}}
    } for n in delete]})
    print(f"Removed {len(delete)} duplicate row(s).")
    return len(delete)


def save_jobs(spreadsheet, records):
    """Write records to the first free rows (filling any gaps), de-duplicating by Application URL.

    Existing rows keep their original Discovered At and Applied? values. Returns (added, updated).
    """
    if not records:
        return 0, 0
    ws = spreadsheet.worksheet("Jobs")
    values = ws.get_all_values()
    width = len(JOB_HEADERS)
    url_col = JOB_HEADERS.index("Application URL")
    date_col = JOB_HEADERS.index("Discovered At")
    applied_col = JOB_HEADERS.index("Applied?")

    url_to_row, blanks, last = {}, [], 1   # url_to_row holds job_key AND content_key -> row
    for number, row in enumerate(values[1:], start=2):
        row = row + [""] * (width - len(row))
        if _is_blank(row):
            blanks.append(number)
            continue
        last = number
        for key in (job_key(row[url_col]), content_key(row)):
            if key:
                url_to_row.setdefault(key, number)
    gaps = [n for n in blanks if n < last]
    next_row = last + 1

    updates, added, updated, new_rows = [], 0, 0, set()
    for record in records:
        keys = [k for k in (job_key(record.get("Application URL")), content_key(record)) if k]
        number = next((url_to_row[k] for k in keys if k in url_to_row), None)
        if number is not None and number in new_rows:
            continue   # duplicate of a job already queued in this batch
        was_existing = number is not None
        if number is not None:
            old = values[number - 1] + [""] * (width - len(values[number - 1]))
            record["Discovered At"] = old[date_col] or record.get("Discovered At", "")
            record["Applied?"] = str(old[applied_col]).strip().upper() == "TRUE"
            updated += 1
        else:
            if gaps:
                number = gaps.pop(0)
            else:
                number = next_row
                next_row += 1
            record["Applied?"] = False
            added += 1
        for k in keys:
            url_to_row.setdefault(k, number)
        new_rows.add(number) if number not in new_rows and not was_existing else None
        row = [record.get(header, "") for header in JOB_HEADERS]
        updates.append({"range": f"A{number}:I{number}", "values": [row]})

    needed = max(int(u["range"].split(":")[0][1:]) for u in updates)
    if ws.row_count < needed + 50:
        ws.add_rows(needed + 50 - ws.row_count)
    ws.batch_update(updates, value_input_option="USER_ENTERED")
    return added, updated


def upsert_job(spreadsheet, record):
    added, _ = save_jobs(spreadsheet, [record])
    return "added" if added else "updated"