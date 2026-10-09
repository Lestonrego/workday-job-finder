# AI Job Finder — Workday + Groq + Google Sheets

A GitHub Actions job scanner for internships and full-time fresher/entry-level roles on employer Workday career sites. Groq interprets the candidate's resume and available job details semantically; results are deduplicated in Google Sheets.

## Where to put your resume

Upload your resume as **`resume.pdf` in the repository root**, in the same folder as `scanner.py` and `career_sites.txt`. Replace that PDF whenever your resume changes; the next scheduled scan reads the latest version from the repository automatically. No code edits are needed.

The scanner extracts selectable text from the PDF using `pypdf` and passes it to Groq for evaluation. Use a text-selectable/searchable PDF. An image-only scanned PDF may not contain extractable text and will produce a clear error. Do not commit API keys or Google service-account credentials to the repository.

## How the matching works

The LLM is prompted to understand the resume as a whole, including education, projects, tools, skills, coursework, and transferable experience. It evaluates each job's available description and listing details rather than relying on exact title or keyword matches. It distinguishes mandatory from preferred requirements, interprets experience ranges such as 0–2 years in context, and keeps plausible uncertain matches for review. It must not invent candidate qualifications.

The target is internships and full-time fresher/entry-level roles in India. There is no strict minimum match-score cutoff. Clearly unrelated, senior, out-of-scope, or non-India/unknown-location roles are filtered out. The scanner first reads listing pages, filters for India-like locations, and then fetches full job details for those listings where the employer exposes them. The quality of the evaluation depends on how much job-description text the employer's Workday site makes available.

## 1. Automatic employer discovery and crawling

The scanner first queries public search-engine results for Workday-hosted career sites related to India internships, entry-level, software, data, and machine-learning roles. It merges those candidates with any optional URLs in `career_sites.txt`, verifies candidate sites against Workday's public jobs endpoint, and probes related paths/locales where possible. You do not have to manually list every employer; `career_sites.txt` may remain empty.

For each confirmed career site, the scanner paginates listings until the site reports no more results or the `MAX_JOBS_PER_SITE` safety cap (default 10,000) is reached. `MAX_DISCOVERED_SITES` defaults to 30 and can be changed in the workflow environment. You may optionally add seed URLs to `career_sites.txt` to improve coverage.

**Coverage limitation:** no public search index is a complete directory of all employers using Workday. Search engines may miss companies, throttle automated queries, return stale links, or expose only a subset of jobs. Some employers block automated access or use non-standard Workday configurations. Therefore, this is best-effort automatic discovery, not a guarantee of every Workday vacancy. The Workday corporate homepage is not a central job feed.

## 2. Set up Google Sheets

1. Open your Google spreadsheet and copy the spreadsheet ID from its URL.
2. In Google Cloud Console, create/select a project and enable **Google Sheets API** and **Google Drive API**.
3. Create a service account and download its JSON key.
4. Share the spreadsheet with the service account's `client_email` as an Editor.
5. Keep the service-account JSON private.

The workflow initializes the `Jobs` tab with these columns, in order: `Company Name`, `Job Title`, `Location`, `Employment Type`, `Eligibility Concerns`, `Application URL`, `Discovered At`, `Career Site URL`, `Applied?`. The last column is configured as a checkbox. Jobs are deduplicated by application URL and the `Applied?` value is preserved when a posting is refreshed.

See `GOOGLE_SHEET_SETUP.txt` for additional setup guidance.

## 3. Configure GitHub Actions

Push the project to a private GitHub repository (recommended because it contains your resume and connects to private configuration). In **Settings → Secrets and variables → Actions**, add these repository secrets:

- `GROQ_API_KEY`: your Groq API key
- `GOOGLE_SHEET_ID`: the spreadsheet ID
- `GOOGLE_SERVICE_ACCOUNT_JSON`: the complete service-account JSON contents as a single secret

Optional repository variable:
- `GROQ_MODEL`: a model currently available in your Groq account. The default configured in this project is `llama-3.3-70b-versatile`.

Enable Actions and run **Scheduled Workday Job Scan → Run workflow** once to test. The workflow is scheduled three times daily at approximately 12:00 AM, 12:00 PM, and 6:00 PM India Standard Time (GitHub Actions may start a little late):

- `30 18 * * *` = 12:00 AM IST
- `30 6 * * *` = 12:00 PM IST
- `30 12 * * *` = 6:00 PM IST

## Important limitations

- Automatic discovery uses public search results and cannot guarantee complete global coverage. Optional `career_sites.txt` URLs can improve recall for employers that search engines miss.
- The location filter is intentionally India-focused and conservative. Add city aliases to `is_india_location()` in `scanner.py if you want more Indian locations included.
- Some Workday sites do not expose full descriptions, so the model may need to mark eligibility as uncertain.
- The scanner records opportunities; it does not submit applications automatically.


### Automatic discovery fallback
The scanner searches public search engines first. If they block automated requests or return no usable results, it tries a small built-in set of public Workday career-site seed URLs and validates each against the site's public jobs API. This improves resilience but is not a complete directory of all Workday employers. `career_sites.txt` remains optional and can be left empty.
