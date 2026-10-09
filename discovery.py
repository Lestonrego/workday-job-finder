"""Best-effort discovery of public Workday career sites using public search results.

This is a discovery aid, not a complete global directory: search engines index only a
subset of employers and may rate-limit automated requests.
"""
import re
import time
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9,en-US;q=0.8",
}

QUERIES = [
    'site:myworkdayjobs.com/en-US/External India jobs internship',
    'site:myworkdayjobs.com/en-US/External India software engineer fresher',
    'site:myworkdayjobs.com/en-US/External India graduate software jobs',
    'site:myworkdayjobs.com/en-US/External India data analyst jobs',
    'site:myworkdayjobs.com/en-US/External India machine learning jobs',
    'site:myworkdayjobs.com/en-US/External Bengaluru internship OR entry level',
    'site:myworkdayjobs.com/en-US/External Hyderabad OR Pune OR Chennai jobs India',
    'site:myworkdayjobs.com/en-US/External "India" "Early Career" OR "Intern"',
    'site:myworkdayjobs.com/en-IN/ India careers jobs',
    'site:myworkdayjobs.com "India" "Careers" Workday jobs',
]


# Verified public Workday career-site roots used only when search engines return no results.
# These are seed employers, not an exhaustive directory; discovery still probes their APIs.
FALLBACK_WORKDAY_SITES = [
    "https://alcon.wd5.myworkdayjobs.com/en-US/careers_alcon",
    "https://iqvia.wd1.myworkdayjobs.com/en-US/IQVIA",
    "https://automationanywhere.wd5.myworkdayjobs.com/en-US/AutomationAnywhereJobs",
    "https://wf.wd1.myworkdayjobs.com/en-US/wellsfargojobs",
    "https://wri.wd501.myworkdayjobs.com/en-US/WRI",
    "https://globalfoundries.wd1.myworkdayjobs.com/en-US/External",
    "https://walmart.wd504.myworkdayjobs.com/en-US/walmartexternal",
    "https://collaborative.wd1.myworkdayjobs.com/en-US/AllOpenings",
]


def _unwrap_ddg(href):
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            href = unquote(target)
    return href


def _career_root(url):
    url = _unwrap_ddg(url)
    parsed = urlparse(url)
    host = parsed.netloc.lower().split(":")[0]
    if not host.endswith("myworkdayjobs.com"):
        return ""
    if not re.match(r"^[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com$", host):
        return ""
    parts = [p for p in parsed.path.split("/") if p]
    # Accept standard locale/site URLs and normalize job-detail/search links to the site root.
    locale_index = next((i for i, p in enumerate(parts) if re.match(r"^[a-z]{2}-[A-Z]{2}$", p)), None)
    if locale_index is None or locale_index + 1 >= len(parts):
        return ""
    locale, site = parts[locale_index], parts[locale_index + 1]
    if site.lower() in {"job", "jobs", "search"}:
        return ""
    return f"https://{host}/{locale}/{site}"


def discover_workday_sites_from_search(max_sites=30):
    """Find public Workday career sites using several public search engines.

    Search engines can block automated requests. If they return no results, use a small
    set of known public Workday career-site roots as seeds; workday.py validates each
    seed against its public jobs API before scanning it.
    """
    session = requests.Session()
    found = {}
    max_sites = max(1, int(max_sites))
    providers = [
        ("DuckDuckGo", "https://html.duckduckgo.com/html/", "q"),
        ("Bing", "https://www.bing.com/search", "q"),
    ]
    for provider_name, endpoint, query_param in providers:
        for query in QUERIES:
            if len(found) >= max_sites:
                break
            try:
                response = session.get(
                    endpoint,
                    params={query_param: query},
                    headers=HEADERS,
                    timeout=20,
                )
                if response.status_code in (403, 429):
                    print(f"{provider_name} rate-limited discovery (HTTP {response.status_code}); trying another source.")
                    break
                response.raise_for_status()
                soup = BeautifulSoup(response.text, "html.parser")
                for anchor in soup.select("a.result__a, li.b_algo h2 a, a[href]"):
                    href = anchor.get("href", "")
                    root = _career_root(href)
                    if root:
                        found.setdefault(root.lower(), root)
                        if len(found) >= max_sites:
                            break
            except Exception as exc:
                print(f"{provider_name} discovery failed for one query: {exc}")
            time.sleep(0.5)
        if len(found) >= max_sites:
            break

    if not found:
        print("Public search engines returned no usable Workday sites; adding fallback seed sites for API validation.")
        for url in FALLBACK_WORKDAY_SITES[:max_sites]:
            found.setdefault(url.lower(), url)

    results = list(found.values())[:max_sites]
    print(f"Automatic discovery produced {len(results)} Workday career-site candidate URL(s) for validation.")
    for url in results:
        print(f"Discovery candidate: {url}")
    return results
