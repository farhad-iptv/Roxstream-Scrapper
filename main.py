import re
import json
import logging
import time
from datetime import datetime, timezone
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

# ---------------------------------------------------------------------------
# pip install requests beautifulsoup4 playwright
# then: playwright install chromium
# ---------------------------------------------------------------------------

BASE_URL = "https://roxiestreams.su"
EPG_URL  = "https://epgshare01.online/epgshare01/epg_ripper_DUMMY_CHANNELS.xml.gz"

CATEGORIES = [
    ("/soccer",      "Soccer"),
    ("/mlb",         "MLB"),
    ("/nba",         "NBA"),
    ("/nfl",         "NFL"),
    ("/nhl",         "NHL"),
    ("/fighting",    "Fighting"),
    ("/motorsports", "Motorsports"),
]

TV_INFO = {
    "soccer":      ("Soccer.Dummy.us",         "Soccer"),
    "mlb":         ("MLB.Baseball.Dummy.us",   "Baseball"),
    "nba":         ("NBA.Basketball.Dummy.us", "Basketball"),
    "nfl":         ("Football.Dummy.us",       "Football"),
    "nhl":         ("NHL.Hockey.Dummy.us",     "Hockey"),
    "fighting":    ("Combat.Sports.Dummy.us",  "Combat Sports"),
    "motorsports": ("Racing.Dummy.us",         "Motorsports"),
}

SPORT_EPG = {
    "f1": "Racing.Dummy.us", "motogp": "Racing.Dummy.us",
    "mxgp": "Racing.Dummy.us", "indycar": "Racing.Dummy.us",
    "nascar": "Racing.Dummy.us", "ufc": "UFC.Fight.Pass.Dummy.us",
    "boxing": "Combat.Sports.Dummy.us", "wwe": "PPV.EVENTS.Dummy.us",
}

DEFAULT_GROUP = "General Sports"
DEFAULT_EPG   = "Sports.Rox.us"

# Regex to pull m3u8 URLs from anywhere
M3U8_REGEX = re.compile(
    r'(https?://[^\s"\'<>`\\,\[\]{}|^]+\.m3u8(?:\?[^\s"\'<>`\\,\[\]{}|^]*)?)'
)

# Matches the domains txt filename pattern the site uses
DOMAINS_TXT_REGEX = re.compile(r'fetch\([\'"]([^\'"]*domains[^\'"]*\.txt)[\'"]')

# Matches the streamPath argument passed to getRandomStream(...)
STREAM_PATH_REGEX = re.compile(
    r"getRandomStream\(\s*['\"]([^'\"]+\.m3u8[^'\"]*)['\"]"
)

# Matches: var subdomain = 'something'
SUBDOMAIN_REGEX = re.compile(r"var\s+subdomain\s*=\s*['\"]([^'\"]+)['\"]")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s"
)
log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": BASE_URL,
    "Accept-Language": "en-US,en;q=0.9",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_cat_info(cat_slug):
    info = TV_INFO.get(cat_slug)
    return (info[0], info[1]) if info else (DEFAULT_EPG, DEFAULT_GROUP)


def get_sport_epg(href_slug, default_epg):
    slug = href_slug.strip("/").lower()
    for key, epg_id in SPORT_EPG.items():
        if key in slug:
            return epg_id
    return default_epg


def fetch(url, timeout=15, referer=None):
    try:
        headers = {}
        if referer:
            headers["Referer"] = referer
        r = SESSION.get(url, timeout=timeout, headers=headers)
        r.raise_for_status()
        return r
    except Exception as e:
        log.warning(f"Fetch failed [{url}]: {e}")
        return None


def check_stream(url):
    """Verify the m3u8 URL is reachable."""
    try:
        r = SESSION.head(url, timeout=7, allow_redirects=True)
        if r.status_code == 405:
            r = SESSION.get(url, timeout=7, stream=True)
        return r.status_code == 200
    except Exception:
        return False


def is_live(row_soup):
    tds = row_soup.find_all("td")
    return len(tds) >= 3 and "LIVE" in tds[2].get_text(strip=True).upper()


# ---------------------------------------------------------------------------
# Core: fetch domainsz29.txt (or whatever the txt file is named)
# and build ALL possible stream URLs
# ---------------------------------------------------------------------------

def fetch_domains(domains_txt_url, page_url):
    """
    Download the domains text file and return the list of domain strings.
    Falls back to an empty list on failure.
    """
    log.info(f"      Fetching domains list: {domains_txt_url}")
    r = fetch(domains_txt_url, referer=page_url)
    if not r:
        return []
    domains = [d.strip() for d in r.text.strip().splitlines() if d.strip()]
    log.info(f"      Got {len(domains)} domain(s)")
    return domains


def build_stream_urls(subdomain, domains, stream_path):
    """
    Replicate getRandomStream() but return ALL possible URLs
    (one per domain) instead of one random one.
    """
    urls = []
    for domain in domains:
        urls.append(f"https://{subdomain}.{domain}/{stream_path}")
    if not urls:
        # Mirror the JS fallback
        urls.append(f"https://{subdomain}.shadow-ran.online/{stream_path}")
    return urls


# ---------------------------------------------------------------------------
# Layer 1 – scrape category pages (static HTML)
# ---------------------------------------------------------------------------

def scrape_category_page(cat_path, cat_label):
    cat_slug      = cat_path.strip("/").lower()
    epg_id, group = get_cat_info(cat_slug)
    cat_url       = urljoin(BASE_URL, cat_path)
    events        = []

    log.info(f"── Category: {cat_label}  →  {cat_url}")
    resp = fetch(cat_url)
    if resp is None:
        return events

    soup  = BeautifulSoup(resp.text, "html.parser")
    table = soup.find("table", id="eventsTable")
    if table is None:
        log.warning(f"  No #eventsTable on {cat_url}")
        return events

    tbody = table.find("tbody")
    rows  = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]

    for row in rows:
        cells = row.find_all("td")
        if not cells:
            continue
        a_tag = cells[0].find("a")
        if not a_tag:
            continue
        title = a_tag.get_text(strip=True)
        href  = a_tag.get("href", "").strip()
        if not href or not title:
            continue

        events.append({
            "category":   cat_label,
            "cat_slug":   cat_slug,
            "title":      title,
            "href":       href,
            "stream_url": urljoin(BASE_URL, href),
            "start_time": cells[1].get_text(strip=True) if len(cells) > 1 else "N/A",
            "is_live":    is_live(row),
            "epg_id":     get_sport_epg(href, epg_id),
            "group":      group,
            "streams":    [],
        })

    log.info(f"  Found {len(events)} event(s)")
    return events


# ---------------------------------------------------------------------------
# Layer 2A – Static extraction: parse the event page HTML/JS
#            to find subdomain, domains txt filename, and stream path
# ---------------------------------------------------------------------------

def extract_streams_static(page_url):
    """
    Download the event page with requests, parse the inline <script> blocks
    to find:
      - the domains txt filename  (e.g. domainsz29.txt)
      - var subdomain             (e.g. 'daffodil')
      - the stream path           (e.g. 'fsp.m3u8')

    Then fetch the domains list and build all stream URLs.
    Returns a list of raw (unverified) m3u8 URLs.
    """
    r = fetch(page_url)
    if not r:
        return []

    html = r.text
    soup = BeautifulSoup(html, "html.parser")

    # Collect all inline script text
    scripts_text = "\n".join(
        s.get_text() for s in soup.find_all("script") if s.get_text()
    )

    # 1) Find the domains txt file reference
    domains_txt_match = DOMAINS_TXT_REGEX.search(scripts_text)
    if not domains_txt_match:
        log.debug(f"      No domains txt reference found in {page_url}")
        return []

    domains_txt_file = domains_txt_match.group(1)  # e.g. "domainsz29.txt"

    # Resolve the URL for the txt file relative to the page
    domains_txt_url = urljoin(page_url, domains_txt_file)

    # 2) Find the subdomain variable
    subdomain_match = SUBDOMAIN_REGEX.search(scripts_text)
    subdomain = subdomain_match.group(1) if subdomain_match else "stream"
    log.info(f"      subdomain={subdomain!r}  domains_file={domains_txt_file!r}")

    # 3) Find the stream path (e.g. 'fsp.m3u8')
    stream_path_matches = STREAM_PATH_REGEX.findall(scripts_text)
    if not stream_path_matches:
        log.debug(f"      No stream path found via getRandomStream() in {page_url}")
        return []

    # De-duplicate while preserving order
    stream_paths = list(dict.fromkeys(stream_path_matches))
    log.info(f"      Stream paths: {stream_paths}")

    # 4) Fetch the domains list
    domains = fetch_domains(domains_txt_url, page_url)
    if not domains:
        log.warning(f"      Empty domains list — using fallback only")
        domains = []  # build_stream_urls handles the fallback

    # 5) Build all candidate URLs
    all_urls = []
    for stream_path in stream_paths:
        all_urls.extend(build_stream_urls(subdomain, domains, stream_path))

    return all_urls


# ---------------------------------------------------------------------------
# Layer 2B – Playwright: intercept network traffic for .m3u8
#            (catches anything the static parse missed)
# ---------------------------------------------------------------------------

def extract_streams_playwright(page_obj, page_url):
    """
    Navigate to the event page in a real browser and intercept every
    network request / response for .m3u8 URLs.
    Also scans page source and all iframe sources.
    """
    captured = set()

    def on_request(request):
        if ".m3u8" in request.url:
            captured.update(M3U8_REGEX.findall(request.url))

    def on_response(response):
        if ".m3u8" in response.url:
            captured.update(M3U8_REGEX.findall(response.url))
        content_type = response.headers.get("content-type", "")
        if any(ct in content_type for ct in ["text/html", "javascript", "json"]):
            try:
                body = response.text()
                captured.update(M3U8_REGEX.findall(body))
            except Exception:
                pass

    page_obj.on("request",  on_request)
    page_obj.on("response", on_response)

    log.info(f"    [playwright] {page_url}")

    try:
        page_obj.goto(page_url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        log.warning(f"    Navigation timeout: {page_url}")
    except Exception as e:
        log.warning(f"    Navigation error: {e}")

    # Wait for the domains fetch + player init to complete
    try:
        page_obj.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass

    # Extra wait for slow players
    time.sleep(6)

    # Scan main frame source
    try:
        captured.update(M3U8_REGEX.findall(page_obj.content()))
    except Exception:
        pass

    # Recursively scan all frames
    def scan_frames(frames):
        for frame in frames:
            try:
                captured.update(M3U8_REGEX.findall(frame.content()))
            except Exception:
                pass
            # JS probes inside each frame
            js_probes = [
                "typeof hlsUrl    !== 'undefined' ? hlsUrl    : ''",
                "typeof streamUrl !== 'undefined' ? streamUrl : ''",
                "typeof videoSrc  !== 'undefined' ? videoSrc  : ''",
                # Read the Clappr player source
                """
                (function(){
                    try{
                        if(typeof clapprPlayer!=='undefined' && clapprPlayer.options && clapprPlayer.options.source)
                            return clapprPlayer.options.source;
                    }catch(e){}
                    return '';
                })()
                """,
                # Read from Hls.js instance
                """
                (function(){
                    try{
                        var v=document.querySelector('video');
                        if(v && v.src && v.src.includes('.m3u8')) return v.src;
                    }catch(e){}
                    return '';
                })()
                """,
                # Scan all inline scripts for m3u8 patterns
                """
                (function(){
                    try{
                        var ss=document.querySelectorAll('script');
                        for(var s of ss){
                            var m=s.textContent.match(/https?:\\/\\/[^\\s"'<>`]+\\.m3u8[^\\s"'<>`]*/);
                            if(m) return m[0];
                        }
                    }catch(e){}
                    return '';
                })()
                """,
            ]
            for js in js_probes:
                try:
                    val = frame.evaluate(js)
                    if val and isinstance(val, str) and ".m3u8" in val:
                        captured.update(M3U8_REGEX.findall(val))
                except Exception:
                    pass
            scan_frames(frame.child_frames)

    scan_frames(page_obj.main_frame.child_frames)

    try:
        page_obj.remove_listener("request",  on_request)
        page_obj.remove_listener("response", on_response)
    except Exception:
        pass

    return list(captured)


# ---------------------------------------------------------------------------
# Layer 2C – Playwright: intercept the domains txt fetch itself
#            and reconstruct all URLs (belt-and-suspenders approach)
# ---------------------------------------------------------------------------

def extract_streams_playwright_domains(page_obj, page_url):
    """
    Same as above but also intercepts the domains txt response so we can
    build the full matrix of all domain × stream-path combinations,
    matching exactly what the JS does.
    """
    captured_m3u8   = set()
    captured_domains = []
    captured_meta    = {
        "subdomain":    None,
        "stream_paths": [],
    }

    def on_response(response):
        url = response.url
        # Intercept the domains txt file
        if re.search(r'domains[^/]*\.txt', url, re.I):
            try:
                text = response.text()
                lines = [l.strip() for l in text.splitlines() if l.strip()]
                captured_domains.extend(lines)
                log.info(f"      [intercept] domains txt → {len(lines)} domain(s)")
            except Exception:
                pass

        # Intercept any direct m3u8 request
        if ".m3u8" in url:
            captured_m3u8.update(M3U8_REGEX.findall(url))

        content_type = response.headers.get("content-type", "")
        if any(ct in content_type for ct in ["text/html", "javascript", "json"]):
            try:
                body = response.text()
                captured_m3u8.update(M3U8_REGEX.findall(body))
                # Also try to pull subdomain / stream path from JS responses
                sd = SUBDOMAIN_REGEX.search(body)
                if sd and not captured_meta["subdomain"]:
                    captured_meta["subdomain"] = sd.group(1)
                for sp in STREAM_PATH_REGEX.findall(body):
                    if sp not in captured_meta["stream_paths"]:
                        captured_meta["stream_paths"].append(sp)
            except Exception:
                pass

    page_obj.on("response", on_response)

    log.info(f"    [playwright-domains] {page_url}")
    try:
        page_obj.goto(page_url, wait_until="domcontentloaded", timeout=30000)
    except PWTimeout:
        log.warning(f"    Timeout: {page_url}")
    except Exception as e:
        log.warning(f"    Nav error: {e}")

    # Wait for the fetch('domainsz29.txt') to complete
    try:
        page_obj.wait_for_load_state("networkidle", timeout=20000)
    except PWTimeout:
        pass

    time.sleep(6)

    # Also pull metadata from the page's own JS after render
    try:
        page_source = page_obj.content()
        sd = SUBDOMAIN_REGEX.search(page_source)
        if sd and not captured_meta["subdomain"]:
            captured_meta["subdomain"] = sd.group(1)
        for sp in STREAM_PATH_REGEX.findall(page_source):
            if sp not in captured_meta["stream_paths"]:
                captured_meta["stream_paths"].append(sp)
        captured_m3u8.update(M3U8_REGEX.findall(page_source))
    except Exception:
        pass

    # Ask the browser itself for the values (live JS context)
    js_extract = """
    (function(){
        var result = { subdomain: '', streamPaths: [], domains: [] };
        try{ result.subdomain = (typeof subdomain !== 'undefined') ? subdomain : ''; }catch(e){}
        try{ result.domains   = (typeof domains   !== 'undefined') ? domains   : []; }catch(e){}
        return result;
    })()
    """
    try:
        js_result = page_obj.evaluate(js_extract)
        if js_result.get("subdomain") and not captured_meta["subdomain"]:
            captured_meta["subdomain"] = js_result["subdomain"]
        if js_result.get("domains"):
            for d in js_result["domains"]:
                if d not in captured_domains:
                    captured_domains.append(d)
        log.info(
            f"      JS context → subdomain={js_result.get('subdomain')!r}  "
            f"domains={len(js_result.get('domains', []))}"
        )
    except Exception as e:
        log.debug(f"      JS extract error: {e}")

    try:
        page_obj.remove_listener("response", on_response)
    except Exception:
        pass

    # Build all domain × path combinations
    synthetic = []
    if captured_meta["subdomain"] and captured_domains and captured_meta["stream_paths"]:
        for path in captured_meta["stream_paths"]:
            synthetic.extend(
                build_stream_urls(
                    captured_meta["subdomain"],
                    captured_domains,
                    path
                )
            )
        log.info(
            f"      Built {len(synthetic)} synthetic URL(s) from "
            f"{len(captured_domains)} domain(s) × "
            f"{len(captured_meta['stream_paths'])} path(s)"
        )

    return list(captured_m3u8) + synthetic


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # ── Step 1: collect all events ────────────────────────────────────────
    all_events = []
    for cat_path, cat_label in CATEGORIES:
        all_events.extend(scrape_category_page(cat_path, cat_label))

    log.info(f"\nTotal events: {len(all_events)}")

    # ── Step 2: extract streams ───────────────────────────────────────────
    stream_cache = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-web-security",
                "--disable-features=IsolateOrigins",
                "--disable-site-isolation-trials",
            ]
        )

        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1920, "height": 1080},
            ignore_https_errors=True,
        )

        # Block only heavy assets — keep JS / XHR / fetch alive
        BLOCK_PATTERNS = [
            "doubleclick.net", "googlesyndication", "google-analytics",
            "googletagmanager", "facebook.net", "adservice",
            "amazon-adsystem", "outbrain", "taboola",
            ".png", ".jpg", ".jpeg", ".gif", ".svg",
            ".woff", ".woff2", ".ttf",
            # NOTE: do NOT block .css or .txt — the domains file is a .txt!
        ]

        def route_handler(route):
            url = route.request.url
            if any(pat in url for pat in BLOCK_PATTERNS):
                route.abort()
            else:
                route.continue_()

        context.route("**/*", route_handler)
        page = context.new_page()

        for event in all_events:
            url = event["stream_url"]

            if url in stream_cache:
                log.info(f"  ▶ (cached) {event['title']}")
                event["streams"] = stream_cache[url]
                continue

            log.info(f"\n  ▶ {event['title']}")
            log.info(f"    {url}")

            raw = set()

            # ── Pass A: static HTML parse (fast, no browser needed) ──────
            static_urls = extract_streams_static(url)
            if static_urls:
                log.info(f"    [static] Found {len(static_urls)} candidate(s)")
                raw.update(static_urls)

            # ── Pass B: Playwright with domain interception ───────────────
            playwright_urls = extract_streams_playwright_domains(page, url)
            if playwright_urls:
                log.info(
                    f"    [playwright] Found {len(playwright_urls)} candidate(s) "
                    f"({len(set(playwright_urls) - raw)} new)"
                )
                raw.update(playwright_urls)

            # ── Pass C: generic Playwright scan (belt-and-suspenders) ─────
            if not raw:
                log.info(f"    [playwright-generic] Running fallback scan …")
                generic_urls = extract_streams_playwright(page, url)
                raw.update(generic_urls)

            # ── Verify each candidate ─────────────────────────────────────
            verified = []
            if raw:
                log.info(f"    Verifying {len(raw)} candidate(s) …")
                for link in sorted(raw):
                    if check_stream(link):
                        log.info(f"    ✔ {link}")
                        verified.append(link)
                    else:
                        log.debug(f"    ✗ offline: {link}")
            else:
                log.warning(f"    No candidates found for: {event['title']}")

            stream_cache[url] = verified
            event["streams"]  = verified

        browser.close()
        log.info("\nBrowser closed.")

    # ── Step 3: write M3U playlist ────────────────────────────────────────
    playlist_lines = [f'#EXTM3U x-tvg-url="{EPG_URL}"']
    seen_streams   = set()
    title_counter  = {}

    for event in all_events:
        for link in event["streams"]:
            if link in seen_streams:
                continue

            title = event["title"]
            title_counter[title] = title_counter.get(title, 0) + 1
            count   = title_counter[title]
            display = title if count == 1 else f"{title} (Mirror {count - 1})"

            playlist_lines.append(
                f'#EXTINF:-1 '
                f'tvg-id="{event["epg_id"]}" '
                f'group-title="{event["group"]}",'
                f'{display}'
            )
            playlist_lines.append(link)
            seen_streams.add(link)

    with open("Roxiestreams.m3u", "w", encoding="utf-8") as f:
        f.write("\n".join(playlist_lines))
    log.info(f"\nPlaylist saved → Roxiestreams.m3u  ({len(seen_streams)} streams)")

    # ── Step 4: write JSON schedule ───────────────────────────────────────
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    json_output = [
        {
            "category":    ev["category"],
            "title":       ev["title"],
            "start_time":  ev["start_time"],
            "is_live":     ev["is_live"],
            "stream_page": ev["stream_url"],
            "group":       ev["group"],
            "epg_id":      ev["epg_id"],
            "streams":     ev["streams"],
            "scraped_at":  now,
        }
        for ev in all_events
    ]

    with open("Roxiestreams_schedule.json", "w", encoding="utf-8") as f:
        json.dump(json_output, f, indent=2, ensure_ascii=False)
    log.info(
        f"Schedule saved → Roxiestreams_schedule.json  ({len(json_output)} events)"
    )

    # ── Summary ───────────────────────────────────────────────────────────
    live_count  = sum(1 for e in all_events if e["is_live"])
    with_stream = sum(1 for e in all_events if e["streams"])
    log.info(
        f"\n{'='*55}\n"
        f"  Categories         : {len(CATEGORIES)}\n"
        f"  Events             : {len(all_events)}\n"
        f"  Events with stream : {with_stream}\n"
        f"  Live now           : {live_count}\n"
        f"  Total streams      : {len(seen_streams)}\n"
        f"{'='*55}"
    )


if __name__ == "__main__":
    main()
