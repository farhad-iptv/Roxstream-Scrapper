import httpx
import json
import re
import asyncio
import datetime
import xml.etree.ElementTree as ET
from xml.dom import minidom
import os
import time
import warnings

warnings.filterwarnings("ignore")

# ────────────────────────────────────────────────
# Configuration
# ────────────────────────────────────────────────

DEFAULT_LOGO    = "https://nowstreams.top/favicon.ico"

EPG_FILENAME    = "epg.xml"
M3U_FILENAME    = "nowstreams.m3u"
M3U8_FILENAME   = "nowstreams.m3u8"
STREAMS_JSON    = "nowstreams.json"
CATEGORIES_JSON = "nowstreams_categories.json"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

SITE_BASE  = "https://nowstreams.top"
API_URL    = "https://nowstreams.top/api_proxy.php"
KORA_BASE  = "https://s2.kora.st"

SEMAPHORE_LIMIT = 3
HTTP_TIMEOUT    = 30.0

CHROME_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)

# ────────────────────────────────────────────────
# Playwright check
# ────────────────────────────────────────────────

USE_PLAYWRIGHT = False
try:
    from playwright.async_api import async_playwright
    USE_PLAYWRIGHT = True
    print("✅ Playwright available")
except ImportError:
    print("⚠️  Playwright not installed.")
    print("   pip install playwright && python -m playwright install chromium")


# ────────────────────────────────────────────────
# HTTP helpers
# ────────────────────────────────────────────────

def make_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(HTTP_TIMEOUT),
        verify=False,
        follow_redirects=True,
    )


def browser_headers(referer: str = "") -> dict:
    h = {
        "User-Agent":      CHROME_UA,
        "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Connection":      "keep-alive",
        "Sec-Fetch-Dest":  "document",
        "Sec-Fetch-Mode":  "navigate",
        "Sec-Fetch-Site":  "cross-site",
    }
    if referer:
        h["Referer"] = referer
    return h


def api_headers() -> dict:
    return {
        "User-Agent":      CHROME_UA,
        "Accept":          "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin":          SITE_BASE,
        "Referer":         SITE_BASE + "/",
        "Connection":      "keep-alive",
    }


# ────────────────────────────────────────────────
# M3U8 regex
# ────────────────────────────────────────────────

_M3U8_PATTERNS = [
    re.compile(
        r"(https?://[a-zA-Z0-9._-]+(?::\d+)?/hls/[^\s\"'<>\\]+\.m3u8(?:\?[^\s\"'<>\\]*)?)",
        re.I,
    ),
    re.compile(
        r"(https?://[^\s\"'<>\\]+\.m3u8(?:\?[^\s\"'<>\\]*)?)",
        re.I,
    ),
    re.compile(
        r"""(?:src|source|file|hls|stream|url|link|video)\s*[=:]\s*['"]"""
        r"""(https?://[^\s'"<>\\]+\.m3u8(?:\?[^\s'"<>\\]*)?)['"]""",
        re.I,
    ),
]


def extract_m3u8_urls(text: str) -> list:
    found = []
    for pat in _M3U8_PATTERNS:
        found.extend(pat.findall(text))
    cleaned = [u.replace("\\/", "/").replace("\\", "") for u in found]
    seen, out = set(), []
    for u in cleaned:
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


# ────────────────────────────────────────────────
# Step 1 — Fetch API
# ────────────────────────────────────────────────

async def fetch_events(client: httpx.AsyncClient) -> list:
    print(f"  📡 GET {API_URL}")
    try:
        r = await client.get(API_URL, headers=api_headers())
        r.raise_for_status()

        try:
            data = r.json()
        except Exception:
            raw = r.content
            for enc in ("utf-8", "utf-8-sig", "latin-1", "cp1252"):
                try:
                    data = json.loads(raw.decode(enc))
                    print(f"  ℹ️  Decoded with {enc}")
                    break
                except Exception:
                    continue
            else:
                print(f"  ❌ Could not decode response.")
                return []

        if isinstance(data, dict):
            for key in ["matches", "data", "events", "items", "results"]:
                if key in data and isinstance(data[key], list):
                    print(f"  ✅ Got {len(data[key])} matches (key='{key}')")
                    return data[key]
            print(f"  ❌ Dict keys: {list(data.keys())}")
            return []

        if isinstance(data, list):
            print(f"  ✅ Got {len(data)} matches")
            return data

        return []

    except Exception as e:
        print(f"  ❌ fetch_events: {e}")
        return []


# ────────────────────────────────────────────────
# Step 2 — Parse matches
# ────────────────────────────────────────────────

def parse_matches(matches: list) -> list:
    entries = []

    for match in matches:
        match_name = match.get("matchstr") or match.get("matchText") or "Unknown"
        league     = match.get("league",    "Sports")
        sport      = match.get("sport",     "Sports")
        match_date = match.get("matchDate", "")
        match_time = match.get("time",      "")
        slug       = match.get("slug",      "")
        start_ts   = match.get("startTimestamp", 0)

        if start_ts:
            try:
                dt        = datetime.datetime.fromtimestamp(
                                start_ts / 1000, tz=datetime.timezone.utc)
                start_fmt = dt.strftime("%Y-%m-%d %H:%M UTC")
            except Exception:
                start_fmt = f"{match_date} {match_time}"
        else:
            start_fmt = f"{match_date} {match_time}"

        for ch in match.get("channels", []):
            ch_name  = ch.get("name",     "Unknown")
            ch_lang  = ch.get("language", "EN")
            ch_num   = ch.get("number",   0)
            links    = ch.get("links",    [])
            old_links= ch.get("oldLinks", [])

            embed_url = None
            for lnk in links + old_links:
                if lnk and isinstance(lnk, str) and lnk.startswith("http"):
                    embed_url = lnk
                    break

            if not embed_url:
                continue

            kora_id_m = re.search(r"[?&]id=(\d+)", embed_url)
            kora_id   = kora_id_m.group(1) if kora_id_m else str(ch_num)

            entries.append({
                "id":            f"{slug}_{kora_id}",
                "name":          f"{match_name} [{ch_name}] ({ch_lang})",
                "event_name":    match_name,
                "channel_name":  ch_name,
                "language":      ch_lang,
                "sport":         sport,
                "league":        league,
                "category_name": f"{sport} - {league}",
                "embed_url":     embed_url,
                "kora_id":       kora_id,
                "start":         start_fmt,
                "start_ts":      start_ts,
                "logo":          DEFAULT_LOGO,
            })

    return entries


# ────────────────────────────────────────────────
# Step 3a — Playwright resolver
# ────────────────────────────────────────────────

async def resolve_via_playwright(entries: list) -> dict:
    unique: dict = {}
    for e in entries:
        if e["kora_id"] not in unique:
            unique[e["kora_id"]] = e["embed_url"]

    print(f"\n  🎭 Playwright: {len(unique)} unique kora.st channels")
    results: dict = {}
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-web-security",
            ],
        )
        context = await browser.new_context(
            user_agent=CHROME_UA,
            ignore_https_errors=True,
        )

        async def resolve_one(kora_id: str, embed_url: str):
            async with sem:
                found_url = None
                page      = await context.new_page()

                async def on_response(response):
                    nonlocal found_url
                    if found_url:
                        return
                    if ".m3u8" in response.url:
                        found_url = response.url
                        print(f"    ✅ CH {kora_id:>4s}: {response.url[:75]}")

                page.on("response", on_response)

                try:
                    await page.goto(embed_url, wait_until="domcontentloaded", timeout=20_000)

                    for _ in range(30):
                        if found_url:
                            break
                        await asyncio.sleep(0.5)

                    if not found_url:
                        for sel in ["button", ".vjs-big-play-button", "[class*='play']", "video"]:
                            try:
                                el = await page.query_selector(sel)
                                if el:
                                    await el.click()
                                    for _ in range(20):
                                        if found_url:
                                            break
                                        await asyncio.sleep(0.5)
                                    if found_url:
                                        break
                            except Exception:
                                pass

                    if not found_url:
                        for frame in page.frames:
                            try:
                                hits = extract_m3u8_urls(await frame.content())
                                if hits:
                                    found_url = hits[0]
                                    print(f"    ✅ CH {kora_id:>4s} (HTML): {found_url[:75]}")
                                    break
                            except Exception:
                                pass

                except Exception as e:
                    print(f"    ❌ CH {kora_id:>4s}: {e}")
                finally:
                    await page.close()

                if found_url:
                    results[kora_id] = found_url
                else:
                    print(f"    ❌ CH {kora_id:>4s}: no m3u8")

        await asyncio.gather(*(resolve_one(k, v) for k, v in unique.items()))
        await browser.close()

    print(f"  🎭 Resolved {len(results)}/{len(unique)} via Playwright")
    return results


# ────────────────────────────────────────────────
# Step 3b — httpx fallback
# ────────────────────────────────────────────────

def _abs(href: str, base: str) -> str:
    if href.startswith("//"):   return "https:" + href
    if href.startswith("/"):
        from urllib.parse import urlparse
        p = urlparse(base)
        return f"{p.scheme}://{p.netloc}{href}"
    if href.startswith("http"): return href
    return base.rstrip("/") + "/" + href


async def _fetch(client, url, ref, timeout=12):
    try:
        r = await client.get(url, headers=browser_headers(ref), timeout=timeout)
        return r.text
    except Exception:
        return ""


async def resolve_via_httpx(client: httpx.AsyncClient, entries: list) -> dict:
    unique: dict = {}
    for e in entries:
        if e["kora_id"] not in unique:
            unique[e["kora_id"]] = e["embed_url"]

    print(f"\n  🔧 httpx fallback: {len(unique)} channels")
    results: dict = {}
    sem = asyncio.Semaphore(SEMAPHORE_LIMIT)

    async def resolve_one(kora_id: str, embed_url: str):
        async with sem:
            html = await _fetch(client, embed_url, SITE_BASE + "/")

            # L1 direct
            hits = extract_m3u8_urls(html)
            if hits:
                results[kora_id] = hits[0]
                print(f"    ✅ CH {kora_id:>4s} (L1): {hits[0][:70]}")
                return

            # Inline scripts
            for sc in re.findall(r"<script[^>]*>(.*?)</script>", html, re.S | re.I):
                hits = extract_m3u8_urls(sc)
                if hits:
                    results[kora_id] = hits[0]
                    print(f"    ✅ CH {kora_id:>4s} (inline): {hits[0][:70]}")
                    return

            # External scripts
            for src in re.findall(r'<script[^>]+src=["\']([^"\']+)["\']', html, re.I):
                js = await _fetch(client, _abs(src, embed_url), embed_url)
                hits = extract_m3u8_urls(js)
                if hits:
                    results[kora_id] = hits[0]
                    print(f"    ✅ CH {kora_id:>4s} (JS): {hits[0][:70]}")
                    return

            # Iframes L2 + L3
            for ifr in re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html, re.I):
                ifr_url = _abs(ifr, embed_url)
                html2   = await _fetch(client, ifr_url, embed_url)
                hits    = extract_m3u8_urls(html2)
                if hits:
                    results[kora_id] = hits[0]
                    print(f"    ✅ CH {kora_id:>4s} (iframe): {hits[0][:70]}")
                    return
                for sub in re.findall(r'<iframe[^>]+src=["\']([^"\']+)["\']', html2, re.I):
                    html3 = await _fetch(client, _abs(sub, ifr_url), ifr_url)
                    hits  = extract_m3u8_urls(html3)
                    if hits:
                        results[kora_id] = hits[0]
                        print(f"    ✅ CH {kora_id:>4s} (L3): {hits[0][:70]}")
                        return

            print(f"    ❌ CH {kora_id:>4s}: no m3u8")

    await asyncio.gather(*(resolve_one(k, v) for k, v in unique.items()))
    print(f"  🔧 httpx resolved {len(results)}/{len(unique)}")
    return results


# ────────────────────────────────────────────────
# Step 4 — Build stream list
# ────────────────────────────────────────────────

async def build_streams(entries: list) -> list:
    resolved: dict = {}

    if USE_PLAYWRIGHT:
        resolved = await resolve_via_playwright(entries)

    remaining = [e for e in entries if e["kora_id"] not in resolved]
    if remaining:
        async with make_client() as client:
            resolved.update(await resolve_via_httpx(client, remaining))

    streams, seen, unique = [], set(), []
    for e in entries:
        url = resolved.get(e["kora_id"], "")
        if url and ".m3u8" in url:
            streams.append({**e, "url": url})

    for s in streams:
        key = (s["event_name"], s["kora_id"])
        if key not in seen:
            seen.add(key)
            unique.append(s)

    unique.sort(key=lambda x: x.get("start_ts", 0))
    print(f"\n  ✅ Final valid streams: {len(unique)}")
    return unique


# ────────────────────────────────────────────────
# Output generators
# ────────────────────────────────────────────────

def _epg_ts(dt: datetime.datetime) -> str:
    return dt.strftime("%Y%m%d%H%M%S +0000")


def generate_epg(streams: list, filepath: str):
    root    = ET.Element("tv", attrib={"generator-info-name": "nowstreams extractor"})
    now     = datetime.datetime.now(datetime.timezone.utc)
    seen_ch = set()

    for s in streams:
        ch_id = s["id"]
        if ch_id not in seen_ch:
            seen_ch.add(ch_id)
            ch_el = ET.SubElement(root, "channel", id=ch_id)
            ET.SubElement(ch_el, "display-name", lang="en").text = s["name"]
            ET.SubElement(ch_el, "icon", src=s["logo"])

        st_dt = now
        en_dt = st_dt + datetime.timedelta(hours=3)
        prog  = ET.SubElement(root, "programme",
                              start=_epg_ts(st_dt), stop=_epg_ts(en_dt), channel=ch_id)
        ET.SubElement(prog, "title",    lang="en").text = s["name"]
        ET.SubElement(prog, "category", lang="en").text = s["category_name"]

    pretty = minidom.parseString(
        ET.tostring(root, encoding="unicode")
    ).toprettyxml(indent="  ")
    pretty = "\n".join(l for l in pretty.split("\n") if l.strip())

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(pretty)
    print(f"  💾 EPG   → {os.path.basename(filepath)}")


def generate_m3u(streams: list, filepath: str):
    now   = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    lines = [
        "#EXTM3U",
        f"# Source  : {SITE_BASE}",
        f"# API     : {API_URL}",
        f"# Generated: {now}  |  Streams: {len(streams)}",
        "",
    ]
    for s in streams:
        lines += [
            f'#EXTINF:-1 tvg-id="{s["id"]}" tvg-name="{s["name"]}" '
            f'tvg-logo="{s["logo"]}" group-title="{s["category_name"]}",{s["name"]}',
            f"#EXTVLCOPT:http-user-agent={CHROME_UA}",
            f"#EXTVLCOPT:http-referrer={KORA_BASE}/",
            s["url"],
            "",
        ]
    with open(filepath, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  💾 M3U   → {os.path.basename(filepath)} ({len(streams)} entries)")


def generate_json(streams: list, filepath: str):
    out = {
        "generated_at":  datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "total_streams": len(streams),
        "streams": [
            {
                "id":       s["id"],
                "name":     s["name"],
                "event":    s["event_name"],
                "channel":  s["channel_name"],
                "language": s["language"],
                "sport":    s["sport"],
                "league":   s["league"],
                "start":    s["start"],
                "url":      s["url"],
                "logo":     s["logo"],
            }
            for s in streams
        ],
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  💾 JSON  → {os.path.basename(filepath)}")


def generate_json_categories(streams: list, filepath: str):
    from collections import defaultdict
    grouped = defaultdict(list)
    for s in streams:
        grouped[s["category_name"]].append({
            "name": s["name"], "url": s["url"],
            "start": s["start"], "language": s["language"],
            "channel": s["channel_name"],
        })
    out = {
        "generated_at": datetime.datetime.now(
            datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "categories": dict(sorted(grouped.items())),
    }
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"  💾 CAT   → {os.path.basename(filepath)}")
    for cat, items in sorted(grouped.items()):
        print(f"         🏆 {cat:35s}: {len(items)}")


# ────────────────────────────────────────────────
# Summary
# ────────────────────────────────────────────────

def print_summary(streams: list):
    print(f"\n{'─'*80}")
    print(f"  {'Event':35s} {'Channel':14s} {'Lang':5s} {'Category':20s}")
    print(f"{'─'*80}")
    for s in streams[:60]:
        print(
            f"  {s['event_name'][:33]:35s}"
            f"{s['channel_name'][:12]:14s}"
            f"{s['language'][:4]:5s}"
            f"{s['category_name'][:20]}"
        )
    if len(streams) > 60:
        print(f"  … and {len(streams)-60} more")
    print(f"{'─'*80}")
    print(f"  Total: {len(streams)} streams")


# ────────────────────────────────────────────────
# Main
# ────────────────────────────────────────────────

async def main():
    print("=" * 65)
    print("  🏟️  nowstreams.top → kora.st M3U8 Extractor")
    print("=" * 65)

    t0 = time.time()

    async with make_client() as client:
        matches = await fetch_events(client)

    if not matches:
        print("\n  ❌ No matches found. Exiting.")
        return

    print(f"\n📋 Parsing {len(matches)} matches…")
    entries     = parse_matches(matches)
    unique_ids  = set(e["kora_id"] for e in entries)
    print(f"  📺 Channel entries    : {len(entries)}")
    print(f"  🔑 Unique kora.st IDs : {len(unique_ids)}")

    if not entries:
        print("  ❌ No entries parsed.")
        return

    streams = await build_streams(entries)

    if not streams:
        print("\n  ❌ No streams resolved.")
        return

    print_summary(streams)

    print(f"\n{'─'*65}")
    print(f"  Saving → {BASE_DIR}")
    print(f"{'─'*65}")

    generate_epg(streams,             os.path.join(BASE_DIR, EPG_FILENAME))
    generate_m3u(streams,             os.path.join(BASE_DIR, M3U_FILENAME))
    generate_m3u(streams,             os.path.join(BASE_DIR, M3U8_FILENAME))
    generate_json(streams,            os.path.join(BASE_DIR, STREAMS_JSON))
    generate_json_categories(streams, os.path.join(BASE_DIR, CATEGORIES_JSON))

    print(f"\n{'='*65}")
    print(f"  ✅ Done in {time.time()-t0:.1f}s  |  Streams: {len(streams)}")
    print(f"{'='*65}")


if __name__ == "__main__":
    asyncio.run(main())
