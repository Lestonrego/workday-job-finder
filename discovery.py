"""Broad discovery of public Workday career sites (no fixed employer list needed).

There is no official public directory of Workday employers, so this module combines
several independent public sources and merges the results:

  1. Common Crawl URL index   - every myworkdayjobs.com URL the crawler has seen.
  2. Internet Archive CDX API - every archived myworkdayjobs.com URL.
  3. Search engines           - DuckDuckGo / Bing / Google queries for India internships.
  4. Optional seeds           - known sites, merged in but never the only source.

Every source only yields *candidates*. workday.py validates each candidate against the
site's public jobs API, and scanner.py / matcher.py then filter for India internships.
Each source is wrapped in its own try/except, so one blocked source never stops the rest.
"""
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlparse, parse_qs, unquote

import requests
from bs4 import BeautifulSoup

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
    "Accept-Language": "en-IN,en;q=0.9,en-US;q=0.8",
}

HOST_RE = re.compile(r"^[a-z0-9-]+\.wd\d+\.myworkdayjobs\.com$")
LOCALE_RE = re.compile(r"^[a-z]{2}-[A-Z]{2}$")
# Path segments that are never a career-site name.
RESERVED_SEGMENTS = {
    "job", "jobs", "search", "wday", "login", "userhome", "account", "assets",
    "favicon.ico", "robots.txt", "sitemap.xml", "details", "apply", "authgwy",
}

# Search-engine queries (India / internship focused).
QUERIES = [
    'site:myworkdayjobs.com/en-US/ India internship',
    'site:myworkdayjobs.com/en-IN/ India jobs',
    'site:myworkdayjobs.com "India" intern',
    'site:myworkdayjobs.com India internship 2026',
    'site:myworkdayjobs.com India summer intern',
    'site:myworkdayjobs.com India fresher OR graduate OR entry level',
    'site:myworkdayjobs.com India trainee OR apprentice',
    'site:myworkdayjobs.com India software engineer intern',
    'site:myworkdayjobs.com India data analyst OR data science intern',
    'site:myworkdayjobs.com India machine learning OR AI intern',
    'site:myworkdayjobs.com India campus hiring OR university recruiting',
    'site:myworkdayjobs.com India early career OR new graduate',
    'site:myworkdayjobs.com Bengaluru intern',
    'site:myworkdayjobs.com Bangalore intern',
    'site:myworkdayjobs.com Hyderabad intern',
    'site:myworkdayjobs.com Pune intern',
    'site:myworkdayjobs.com Chennai intern',
    'site:myworkdayjobs.com Mumbai intern',
    'site:myworkdayjobs.com Gurgaon OR Gurugram OR Noida intern',
    'site:myworkdayjobs.com Delhi OR Kolkata OR Ahmedabad intern',
    'site:myworkdayjobs.com Mangalore OR Mysore OR Kochi OR Coimbatore intern',
    'site:myworkdayjobs.com "India" "Associate Software Engineer"',
    'site:myworkdayjobs.com "India" "Graduate Engineer"',
    'site:myworkdayjobs.com "India" "0-2 years"',
]

# Optional extra seeds. They are merged with dynamic results, never used alone
# unless every dynamic source fails.
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


def _env_int(name, default):
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _unwrap(href):
    if href.startswith("//"):
        href = "https:" + href
    parsed = urlparse(href)
    if parsed.netloc.endswith("duckduckgo.com") and parsed.path.startswith("/l/"):
        target = parse_qs(parsed.query).get("uddg", [""])[0]
        if target:
            href = unquote(target)
    return href


def _host_of(url):
    try:
        host = urlparse(url).netloc.lower().split(":")[0]
    except Exception:
        return ""
    return host if HOST_RE.match(host) else ""


def _career_root(url):
    """Return https://host/locale/site for a Workday URL, or '' if it has no site path."""
    url = _unwrap(url)
    host = _host_of(url)
    if not host:
        return ""
    parts = [p for p in urlparse(url).path.split("/") if p]
    if not parts:
        return ""
    locale = "en-US"
    if LOCALE_RE.match(parts[0]):
        locale = parts[0]
        parts = parts[1:]
    if not parts:
        return ""
    site = parts[0]
    if site.lower() in RESERVED_SEGMENTS or "." in site:
        return ""
    return f"https://{host}/{locale}/{site}"


# --------------------------------------------------------------------------- sources

def _discover_common_crawl(found, hosts, max_sites):
    """Read myworkdayjobs.com URLs from the latest Common Crawl indexes."""
    crawls = _env_int("CC_CRAWLS", 2)
    max_pages = _env_int("CC_MAX_PAGES", 25)
    if not crawls or not max_pages:
        return
    try:
        info = requests.get("https://index.commoncrawl.org/collinfo.json", headers=HEADERS, timeout=30)
        info.raise_for_status()
        api_urls = [c["cdx-api"] for c in info.json()[:crawls]]
    except Exception as exc:
        print(f"Common Crawl: could not list crawls: {exc}")
        return

    for api in api_urls:
        if len(found) >= max_sites:
            return
        params = {"url": "*.myworkdayjobs.com", "output": "json", "fl": "url"}
        try:
            probe = requests.get(api, params={**params, "showNumPages": "true"}, headers=HEADERS, timeout=60)
            pages = int(probe.json().get("pages", 1)) if probe.status_code == 200 else 1
        except Exception:
            pages = 1
        pages = min(pages, max_pages)
        print(f"Common Crawl {api.rsplit('/', 1)[-1]}: reading {pages} index page(s).")
        for page in range(pages):
            if len(found) >= max_sites:
                return
            try:
                response = requests.get(api, params={**params, "page": page}, headers=HEADERS, timeout=120)
                if response.status_code != 200:
                    print(f"Common Crawl page {page}: HTTP {response.status_code}")
                    continue
                before = len(found)
                for line in response.text.splitlines():
                    try:
                        url = json.loads(line).get("url", "")
                    except ValueError:
                        continue
                    _add_candidate(url, found, hosts)
                print(f"Common Crawl page {page + 1}/{pages}: {len(found) - before} new site(s).")
            except Exception as exc:
                print(f"Common Crawl page {page} failed: {exc}")
            time.sleep(0.5)


def _discover_wayback(found, hosts, max_sites):
    """Read archived myworkdayjobs.com URLs from the Internet Archive CDX API."""
    limit = _env_int("WAYBACK_LIMIT", 100000)
    if not limit or len(found) >= max_sites:
        return
    try:
        response = requests.get(
            "https://web.archive.org/cdx/search/cdx",
            params={
                "url": "*.myworkdayjobs.com",
                "output": "txt",
                "fl": "original",
                "collapse": "urlkey",
                "filter": "statuscode:200",
                "limit": str(limit),
            },
            headers=HEADERS,
            timeout=180,
        )
        if response.status_code != 200:
            print(f"Wayback CDX: HTTP {response.status_code}")
            return
        before = len(found)
        for line in response.text.splitlines():
            _add_candidate(line.strip(), found, hosts)
            if len(found) >= max_sites:
                break
        print(f"Wayback CDX: {len(found) - before} new site(s).")
    except Exception as exc:
        print(f"Wayback CDX failed: {exc}")


def _extract_links(soup, provider_name):
    if provider_name == "DuckDuckGo":
        anchors = soup.select("a.result__a, a[href]")
    else:
        anchors = soup.select("li.b_algo h2 a, a[href]")
    for anchor in anchors:
        yield anchor.get("href", "")


def _discover_search_engines(found, hosts, max_sites):
    session = requests.Session()
    pages_per_query = max(1, _env_int("DISCOVERY_PAGES_PER_QUERY", 3))
    providers = [
        ("DuckDuckGo", "https://html.duckduckgo.com/html/", "q", "s", 30, 0),
        ("Bing", "https://www.bing.com/search", "q", "first", 10, 1),
        ("Google", "https://www.google.com/search", "q", "start", 10, 0),
    ]
    for provider_name, endpoint, query_param, page_param, page_step, page_start in providers:
        provider_blocked = False
        for qi, query in enumerate(QUERIES, start=1):
            if len(found) >= max_sites or provider_blocked:
                break
            for page in range(pages_per_query):
                try:
                    response = session.get(
                        endpoint,
                        params={query_param: query, page_param: page * page_step + page_start},
                        headers=HEADERS,
                        timeout=20,
                    )
                    if response.status_code in (403, 429):
                        print(f"{provider_name} rate-limited (HTTP {response.status_code}); next provider.")
                        provider_blocked = True
                        break
                    response.raise_for_status()
                    soup = BeautifulSoup(response.text, "html.parser")
                    before = len(found)
                    for href in _extract_links(soup, provider_name):
                        _add_candidate(href, found, hosts)
                    print(f"Search {provider_name}: query {qi}/{len(QUERIES)}, page {page + 1}, {len(found) - before} new site(s).")
                    if len(found) == before and page > 0:
                        break
                except Exception as exc:
                    print(f"{provider_name} search failed: {exc}")
                    break
                time.sleep(0.35)


def _add_candidate(url, found, hosts):
    """Record a URL's career root if it has one; always remember its Workday host."""
    url = _unwrap(url)
    host = _host_of(url)
    if not host:
        return
    hosts.add(host)
    root = _career_root(url)
    if root:
        found.setdefault(root.lower(), root)


def _resolve_host(host):
    """Follow a bare tenant host's redirect to find its default career-site path."""
    try:
        response = requests.get(f"https://{host}/", headers=HEADERS, timeout=12, allow_redirects=True)
        return _career_root(response.url)
    except Exception:
        return ""


def _resolve_bare_hosts(found, hosts, max_sites):
    """Tenants seen only as a host (no site path) get resolved via their root redirect."""
    covered = {urlparse(u).netloc.lower() for u in found.values()}
    bare = sorted(h for h in hosts if h not in covered)
    cap = _env_int("MAX_HOST_RESOLVE", 3000)
    bare = bare[:cap]
    if not bare:
        return
    print(f"Resolving default career site for {len(bare)} tenant host(s) with no known site path.")
    added = 0
    with ThreadPoolExecutor(max_workers=16) as pool:
        for root in pool.map(_resolve_host, bare):
            if root and len(found) < max_sites:
                if root.lower() not in found:
                    added += 1
                found.setdefault(root.lower(), root)
    print(f"Host resolution added {added} site(s).")


# --------------------------------------------------------------------------- entry point

def discover_workday_sites_from_search(max_sites=5000):
    """Collect candidate Workday career-site URLs from all public sources.

    max_sites is a safety cap. Candidates are validated later by workday.py.
    """
    max_sites = max(1, int(max_sites))
    found = {}
    hosts = set()

    for name, source in (
        ("Common Crawl", _discover_common_crawl),
        ("Internet Archive", _discover_wayback),
        ("Search engines", _discover_search_engines),
    ):
        try:
            source(found, hosts, max_sites)
        except Exception as exc:
            print(f"{name} discovery crashed and was skipped: {exc}")
        print(f"After {name}: {len(found)} site(s), {len(hosts)} tenant host(s).")

    try:
        _resolve_bare_hosts(found, hosts, max_sites)
    except Exception as exc:
        print(f"Host resolution skipped: {exc}")

    # Seeds are additive: they supplement dynamic discovery, they never replace it.
    seed_added = 0
    for url in FALLBACK_WORKDAY_SITES:
        if len(found) >= max_sites:
            break
        if url.lower() not in found:
            found[url.lower()] = url
            seed_added += 1
    print(f"Added {seed_added} seed site(s); {len(found)} candidate(s) before API validation.")

    results = list(found.values())[:max_sites]
    print(f"Automatic discovery produced {len(results)} Workday career-site candidate URL(s).")
    return results
