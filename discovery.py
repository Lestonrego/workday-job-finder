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
    """Search the public web for Workday-hosted career sites relevant to India roles."""
    session = requests.Session()
    found = {}
    max_sites = max(1, int(max_sites))
    for query in QUERIES:
        if len(found) >= max_sites:
            break
        try:
            response = session.get(
                "https://html.duckduckgo.com/html/",
                params={"q": query},
                headers=HEADERS,
                timeout=20,
            )
            if response.status_code in (403, 429):
                print(f"Search provider rate-limited discovery (HTTP {response.status_code}); keeping results found so far.")
                break
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            for anchor in soup.select("a.result__a, a[href]"):
                href = anchor.get("href", "")
                root = _career_root(href)
                if root:
                    found.setdefault(root.lower(), root)
                    if len(found) >= max_sites:
                        break
        except Exception as exc:
            print(f"Public search discovery failed for one query: {exc}")
        time.sleep(1.0)
    results = list(found.values())
    print(f"Automatic discovery found {len(results)} distinct Workday career-site URL(s).")
    for url in results:
        print(f"Discovered candidate: {url}")
    return results
