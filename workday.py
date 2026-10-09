"""Workday career-site discovery and exhaustive public listing pagination."""
import re
import time
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
}
PAGE_SIZE = 20

# Workday tenants can publish different career sites under the same host.
# These are candidates only: a site is added only if its public jobs endpoint responds.
COMMON_SITE_NAMES = [
    "External", "External_Career_Site", "External_Careers", "ExternalCareers",
    "Careers", "Jobs", "Campus", "Internships", "Students", "University_Recruiting",
    "Early_Career", "Early_Careers", "External_Global", "External_US", "Internal",
]
COMMON_LOCALES = ["en-US", "en-IN", "en-GB", "en-CA"]


def normalize_url(url):
    url = url.strip()
    if not url:
        return ""
    if "://" not in url:
        url = "https://" + url
    parsed = urlparse(url)
    return parsed._replace(query="", fragment="").geturl().rstrip("/")


def parse_workday_url(url):
    url = normalize_url(url)
    parsed = urlparse(url)
    host = parsed.netloc.split(":")[0]
    if "myworkdayjobs.com" not in host.lower():
        raise ValueError("Not a standard Workday-hosted career URL")
    tenant_match = re.match(r"([^.]+)\.wd\d+\.myworkdayjobs\.com$", host, re.I)
    if not tenant_match:
        raise ValueError("Could not determine Workday tenant from host")
    tenant = tenant_match.group(1)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise ValueError("URL must include a career-site path, e.g. /en-US/External")
    if re.match(r"^[a-z]{2}-[A-Z]{2}$", parts[0]) and len(parts) > 1:
        locale, site = parts[0], parts[1]
    else:
        locale, site = "en-US", parts[0]
    base = f"https://{host}"
    api = f"{base}/wday/cxs/{tenant}/{site}/jobs"
    return {"tenant": tenant, "site": site, "locale": locale, "base": base, "api": api}


def _api_has_jobs(cfg, session):
    try:
        response = session.post(
            cfg["api"],
            json={"appliedFacets": {}, "limit": 1, "offset": 0, "searchText": ""},
            headers=HEADERS,
            timeout=12,
        )
        if response.status_code != 200:
            return False
        data = response.json()
        return isinstance(data, dict) and ("jobPostings" in data or "total" in data)
    except Exception:
        return False


def discover_workday_sites(seed_urls):
    """Discover linked/common career-site paths for each supplied tenant.

    No global employer directory exists here. Seed URLs identify employers/tenants;
    this function tries to find additional public sites belonging to those tenants.
    """
    session = requests.Session()
    discovered = {}
    queue = [normalize_url(url) for url in seed_urls if normalize_url(url)]
    checked_hosts = set()
    visited = set()
    probed_sites = set()

    while queue:
        url = queue.pop(0)
        try:
            cfg = parse_workday_url(url)
        except ValueError as exc:
            print(f"Ignoring invalid Workday URL {url}: {exc}")
            continue
        key = (cfg["base"].lower(), cfg["locale"].lower(), cfg["site"].lower())
        if key in visited:
            continue
        visited.add(key)
        probed_sites.add(key)
        if _api_has_jobs(cfg, session):
            discovered[key] = url
            print(f"Confirmed Workday career site: {url}")

        # Inspect the supplied career page for links to other career sites on this host.
        if cfg["base"] not in checked_hosts:
            checked_hosts.add(cfg["base"])
            try:
                page = session.get(url, headers=HEADERS, timeout=15)
                if page.status_code == 200:
                    soup = BeautifulSoup(page.text, "html.parser")
                    for anchor in soup.select("a[href]"):
                        href = urljoin(url, anchor.get("href", ""))
                        parsed = urlparse(href)
                        if parsed.netloc.lower() != urlparse(cfg["base"]).netloc.lower():
                            continue
                        parts = [part for part in parsed.path.split("/") if part]
                        if len(parts) >= 2 and re.match(r"^[a-z]{2}-[A-Z]{2}$", parts[0]):
                            candidate = f"{cfg['base']}/{parts[0]}/{parts[1]}"
                            if candidate.rstrip("/") != url.rstrip("/"):
                                queue.append(candidate)
            except Exception as exc:
                print(f"Could not inspect career page links for {url}: {exc}")

        # Probe common site names and locale paths on this same tenant. Invalid paths are ignored.
        # Only a site with a working public jobs endpoint is kept.
        if key not in discovered:
            continue
        for locale in COMMON_LOCALES:
            for site_name in COMMON_SITE_NAMES:
                candidate_key = (cfg["base"].lower(), locale.lower(), site_name.lower())
                if candidate_key in discovered:
                    continue
                candidate_url = f"{cfg['base']}/{locale}/{site_name}"
                if candidate_key in probed_sites:
                    continue
                probed_sites.add(candidate_key)
                candidate_cfg = parse_workday_url(candidate_url)
                if _api_has_jobs(candidate_cfg, session):
                    discovered[candidate_key] = candidate_url
                    print(f"Discovered additional Workday site: {candidate_url}")
                time.sleep(0.03)

    return list(discovered.values())


def fetch_job_detail(cfg, external_path, session):
    """Fetch a public Workday job detail, including its full description when exposed."""
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
    for attempt in range(3):
        try:
            response = session.get(detail_url, headers=HEADERS, timeout=20)
            if response.status_code == 429 or response.status_code >= 500:
                time.sleep(1.5 * (attempt + 1))
                continue
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
            time.sleep(0.8 * (attempt + 1))
    return {}


def enrich_workday_job(job):
    """Fetch full details only after the scanner has confirmed an India location."""
    try:
        cfg = parse_workday_url(job.get("career_site", ""))
        session = requests.Session()
        detail = fetch_job_detail(cfg, job.get("posting_url", ""), session)
        if detail:
            job["title"] = detail.get("detail_title") or job.get("title", "")
            job["location"] = detail.get("detail_location") or job.get("location", "")
            job["employment_type"] = detail.get("employment_type") or job.get("employment_type", "")
            job["experience_requirement"] = detail.get("experience_requirement") or ""
            job["description"] = detail.get("description") or ""
            job["raw"] = {"listing": job.get("raw", {}), "detail": detail.get("detail_raw", {}), "description": job.get("description", "")}
        return job
    except Exception as exc:
        print(f"Could not enrich job details for {job.get('posting_url', '')}: {exc}")
        return job


def fetch_workday_jobs(career_url, max_jobs=100000):
    """Fetch pages until Workday returns no listings or the safety cap is reached.

    Some Workday tenants incorrectly report total=0 on later pages, so a zero or
    missing total must not terminate pagination while postings are still returned.
    """
    cfg = parse_workday_url(career_url)
    results = []
    offset = 0
    session = requests.Session()
    max_jobs = max(1, int(max_jobs))

    while offset < max_jobs:
        payload = {"appliedFacets": {}, "limit": PAGE_SIZE, "offset": offset, "searchText": ""}
        response = None
        max_attempts = 8
        for attempt in range(max_attempts):
            try:
                response = session.post(cfg["api"], json=payload, headers=HEADERS, timeout=30)
                if response.status_code == 429 or response.status_code >= 500:
                    wait = min(60, 2 ** attempt)
                    retry_after = response.headers.get("Retry-After")
                    if retry_after:
                        try:
                            wait = max(wait, min(120, float(retry_after)))
                        except ValueError:
                            pass
                    print(f"  Workday API returned {response.status_code}; retry {attempt + 1}/{max_attempts} in {wait:.1f}s")
                    time.sleep(wait)
                    continue
                response.raise_for_status()
                break
            except Exception:
                if attempt == max_attempts - 1:
                    raise
                time.sleep(min(60, 2 ** attempt))
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
            results.append({
                "job_id": job_id,
                "title": title,
                "location": locations,
                "posted_on": item.get("postedOn", ""),
                "posting_url": posting_url,
                "application_url": posting_url,
                "company": cfg["tenant"],
                "career_site": career_url,
                "description": "",
                "experience_requirement": "",
                "employment_type": "",
                "raw": item,
            })
            if len(results) >= max_jobs:
                break

        offset += len(postings)
        total = data.get("total")
        print(f"  Page offset {offset - len(postings)}: {len(postings)} listings; total reported: {total if total is not None else 'unknown'}")
        if len(postings) < PAGE_SIZE:
            break
        # Workday occasionally reports total=0 on offset > 0 despite returning jobs.
        # Only trust a positive total as an end condition.
        if isinstance(total, int) and total > 0 and offset >= total:
            break
        if len(results) >= max_jobs:
            print(f"  Reached MAX_JOBS_PER_SITE safety cap ({max_jobs}). Increase it to scan more listings.")
            break
        # Small pause reduces pressure on public Workday endpoints.
        time.sleep(0.35)

    return results
