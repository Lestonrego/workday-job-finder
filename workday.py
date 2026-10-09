import re
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobFinder/1.0; contact administrator)",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}

def parse_workday_url(url):
    url = url.strip()
    parsed = urlparse(url if "://" in url else "https://" + url)
    host = parsed.netloc.split(":")[0]
    if "myworkdayjobs.com" not in host.lower():
        raise ValueError("Not a standard Workday-hosted career URL")
    tenant_match = re.match(r"([^.]+)\.wd\d+\.myworkdayjobs\.com$", host, re.I)
    if not tenant_match:
        raise ValueError("Could not determine Workday tenant from host")
    tenant = tenant_match.group(1)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise ValueError("URL must include a career site path, e.g. /en-US/External")
    if re.match(r"^[a-z]{2}-[A-Z]{2}$", parts[0]) and len(parts) > 1:
        locale, site = parts[0], parts[1]
    else:
        locale, site = "en-US", parts[0]
    base = f"https://{host}"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    return {"tenant": tenant, "site": site, "locale": locale, "base": base, "api": api}

def fetch_job_detail(cfg, external_path, session):
    """Try the public Workday CXS detail endpoint. Some employers disable or alter it."""
    if not external_path:
        return {}
    parsed_path = urlparse(external_path).path
    marker = "/job/"
    if marker not in parsed_path:
        return {}
    job_slug = parsed_path.split(marker, 1)[1].strip("/")
    if not job_slug:
        return {}
    detail_url = f"{cfg['base']}/wday/cxs/{cfg['tenant']}/{cfg['site']}/job/{job_slug}"
    try:
        response = session.get(detail_url, headers=HEADERS, timeout=20)
        if response.status_code != 200:
            return {}
        data = response.json()
        info = data.get("jobPostingInfo") or data
        description_html = info.get("jobDescription") or info.get("jobDescriptionHtml") or ""
        description = BeautifulSoup(description_html, "html.parser").get_text(" ", strip=True) if description_html else ""
        return {
            "description": description,
            "detail_title": info.get("title") or info.get("jobTitle") or "",
            "detail_location": info.get("location") or info.get("locationName") or "",
            "experience_requirement": info.get("experienceLevel") or "",
            "employment_type": info.get("timeType") or info.get("workerSubType") or "",
            "job_req_id": info.get("jobReqId") or "",
            "detail_raw": info,
        }
    except Exception:
        return {}

def fetch_workday_jobs(career_url, max_jobs=500):
    cfg = parse_workday_url(career_url)
    results = []
    offset = 0
    limit = 20
    session = requests.Session()
    while offset < max_jobs:
        payload = {"appliedFacets": {}, "limit": limit, "offset": offset, "searchText": ""}
        response = session.post(cfg["api"], json=payload, headers=HEADERS, timeout=30)
        response.raise_for_status()
        data = response.json()
        postings = data.get("jobPostings") or []
        if not postings:
            break
        for item in postings:
            path = item.get("externalPath") or item.get("externalUrl") or ""
            if path.startswith("http"):
                posting_url = path
                external_path = urlparse(path).path
            else:
                external_path = path
                posting_url = cfg["base"] + (path if path.startswith("/") else "/" + path)
            locations = item.get("locationsText") or item.get("location") or ""
            title = item.get("title") or item.get("jobTitle") or ""
            bullets = item.get("bulletFields") or []
            job_id = str(bullets[0] if bullets else item.get("id", "")) or posting_url
            detail = fetch_job_detail(cfg, external_path, session)
            results.append({
                "job_id": detail.get("job_req_id") or job_id or posting_url,
                "title": detail.get("detail_title") or title,
                "location": detail.get("detail_location") or locations,
                "posted_on": item.get("postedOn", ""),
                "posting_url": posting_url,
                "application_url": posting_url,
                "company": cfg["tenant"],
                "career_site": career_url,
                "description": detail.get("description", ""),
                "experience_requirement": detail.get("experience_requirement", ""),
                "employment_type": detail.get("employment_type", ""),
                "raw": {"listing": item, "detail": detail.get("detail_raw", {})},
            })
        offset += len(postings)
        total = data.get("total")
        if len(postings) < limit or (isinstance(total, int) and offset >= total):
            break
    return results[:max_jobs]
