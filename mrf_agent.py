#!/usr/bin/env python3
"""
mrf_agent.py - autonomous finder for a hospital's machine-readable file
(MRF / chargemaster / standard charges file).

Give it a hospital name (state, city and URL are optional; the URL may be
wrong). It finds the official website itself, finds the file, then OPENS the
file and checks that it really is that hospital's MRF.

Pipeline (cheapest first, Claude only as a last resort):
  1. Web search (free)   -> candidate official websites. Your URL is just
                            one more candidate and is never blindly trusted.
  2. /cms-hpt.txt        -> CMS-required file on every hospital site that
                            lists the MRF URL.
  3. Crawl               -> price-transparency pages -> file links
                            (uses Playwright automatically if installed, for
                            JavaScript-heavy sites).
  4. Verify              -> download the first 128 KB of each candidate and
                            check it is an MRF and matches the hospital name.
  5. Claude web search   -> only if 1-4 fail and ANTHROPIC_API_KEY is set.

Install : pip install requests beautifulsoup4 ddgs
Geo-blocked? (site says "access denied" outside the US)
          run from a US machine, or: --proxy http://user:pass@host:port
Optional: pip install curl_cffi        (gets past sites that block Python)
          pip install playwright && playwright install chromium
          BRAVE_API_KEY       better search than the free default
          ANTHROPIC_API_KEY   enables the last-resort Claude fallback

Run one : python mrf_agent.py --name "Western Regional Medical Center" --state AZ
Batch   : python mrf_agent.py --csv hospitals.csv --out results.jsonl
          (CSV columns: name,state,city,url - only name is required)
"""
import argparse
import csv
import json
import os
import re
import sys
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                         "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
TIMEOUT = 20
MODEL = os.getenv("MRF_MODEL", "claude-haiku-5-5")
VERBOSE = False
HEADED = False   # --headed: show the browser window (helps vs. bot blocks)
RENDER_BUDGET = [8]  # max Playwright page loads per run
BLOCKED = set()   # hosts that answered 401/403/429 to every attempt
PROXY = os.getenv("MRF_PROXY", "")   # e.g. http://user:pass@us-proxy:8080
VERIFY = True   # set False with --insecure (corporate SSL proxies)
ALIASES = {"saint": "st", "mount": "mt", "ctr": "center", "centre": "center",
           "med": "medical", "reg": "regional", "hosp": "hospital",
           "mem": "memorial", "univ": "university"}

FILE_EXT = (".json", ".csv", ".zip", ".xlsx", ".xls", ".gz")
PAGE_HINTS = ("price", "transparen", "chargemaster", "standard-charge",
              "standard charge", "standardcharge", "cost-estimat", "cdm")
FILE_HINTS = ("standardcharge", "standard-charge", "standard_charge",
              "standard charges", "chargemaster", "machine-readable",
              "machine readable", "machine_readable", "cdm", "mrf")
GENERIC = {"hospital", "medical", "center", "centre", "health", "healthcare",
           "regional", "system", "clinic", "campus", "emergency"}
SKIP_WORDS = {"the", "of", "and", "inc", "llc"}
BAD_DOMAINS = ("yelp.", "healthgrades.", "usnews.", "facebook.", "linkedin.",
               "wikipedia.", "definitivehc.", "mapquest.", "npino.", "doximity.",
               "webmd.", "zocdoc.", "indeed.", "glassdoor.", "bbb.org",
               "yellowpages.", "cms.gov", "medicare.gov", "leapfrog",
               "vitals.", "birdeye.", "instagram.", "youtube.", "twitter.",
               "x.com", "reddit.", "bing.", "google.", "duckduckgo.",
               "patientpricing", "turquoise", "trustedhealth",
               # third-party price aggregators - never the hospital's own file
               "pricetransparen", "procedureradar", "plainprocedure",
               "medlyze", "healthcare4ppl", "medicarelist", "hospitals.net",
               "pricepoint", "hospitalpric", "healthcarebluebook",
               "clearhealthcosts", "fairhealth", "sidecarhealth")


def is_bad_host(u):
    h = host_key(u) if "//" in u else u.lower()
    return any(b in h for b in BAD_DOMAINS)


def log(*a):
    if VERBOSE:
        print("[mrf]", *a, file=sys.stderr, flush=True)


# ------------------------------------------------------------- text utils
def words(s):
    """Lower-case words with common abbreviations unified (saint -> st)."""
    return [ALIASES.get(w, w) for w in re.findall(r"[a-z0-9]+", s.lower())]


def toks(s):
    return [w for w in words(s) if len(w) > 1 and w not in SKIP_WORDS]


def distinct(s):
    return [w for w in toks(s) if w not in GENERIC]


def name_match(name, text):
    """0..1 - weighted share of the hospital-name words found in `text`.
    Distinctive words (e.g. 'western') count 3x generic ones ('medical')."""
    have = set(words(text))
    num = den = 0
    for w in toks(name):
        wt = 1 if w in GENERIC else 3
        den += wt
        num += wt if w in have else 0
    return num / den if den else 0.0


def name_covers(name, text):
    """True only if EVERY distinctive word of the hospital name is in `text`
    (so 'Rogers Memorial Hospital-Sheboygan' never matches 'Aurora ... Sheboygan')."""
    need = distinct(name)
    if not need:
        return name_match(name, text) >= 0.8
    have = set(words(text))
    return all(t in have for t in need)


def host_key(u):
    return urlparse(u).netloc.lower().removeprefix("www.")


def origin(u):
    p = urlparse(u)
    return f"{p.scheme}://{p.netloc}"


def is_file(u):
    return urlparse(u).path.lower().endswith(FILE_EXT)


# ----------------------------------------------------------------- http
BROWSER_HEADERS = {
    **HEADERS,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


def _curl_get(url, headers, stream):
    """Optional fallback: curl_cffi imitates Chrome's TLS fingerprint."""
    try:
        from curl_cffi import requests as cr
    except ImportError:
        return None
    try:
        return cr.get(url, headers=headers, timeout=TIMEOUT, stream=stream,
                      impersonate="chrome", verify=VERIFY, proxies=_proxies())
    except Exception as e:  # noqa: BLE001
        log("curl_cffi failed", url, type(e).__name__, str(e)[:120])
        return None


def _proxies():
    return {"http": PROXY, "https": PROXY} if PROXY else None


def fetch(url, headers=None, stream=False):
    """GET that never fails silently: logs the reason, retries blocked
    requests with a Chrome-like TLS fingerprint if curl_cffi is installed."""
    h = {**BROWSER_HEADERS, **(headers or {})}
    blocked = True
    try:
        r = requests.get(url, headers=h, timeout=TIMEOUT, stream=stream,
                         verify=VERIFY, proxies=_proxies())
        if r.status_code in (200, 206):
            return r
        log("HTTP", r.status_code, url)
        blocked = r.status_code in (401, 403, 406, 429, 503)
        r.close()
    except requests.RequestException as e:
        log("request failed:", url, type(e).__name__, str(e)[:160])
    if not blocked:
        return None
    alt = _curl_get(url, h, stream)
    if alt is not None and alt.status_code in (200, 206):
        log("curl_cffi worked for", url)
        return alt
    if alt is not None:
        log("curl_cffi HTTP", alt.status_code, url)
    BLOCKED.add(host_key(url))
    return None


def get(url):
    return fetch(url)


def render_html(url):
    """Optional: load the page in a real browser (Playwright). Tries the
    installed Google Chrome first (no download needed), then bundled Chromium."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        log("playwright not installed - cannot render JS / get past bot blocks")
        return None
    if RENDER_BUDGET[0] <= 0:
        return None
    RENDER_BUDGET[0] -= 1
    try:
        with sync_playwright() as p:
            kw = {"proxy": {"server": PROXY}} if PROXY else {}
            try:
                b = p.chromium.launch(channel="chrome", headless=not HEADED, **kw)
            except Exception:  # noqa: BLE001
                b = p.chromium.launch(headless=not HEADED, **kw)
            ctx = b.new_context(user_agent=HEADERS["User-Agent"], locale="en-US")
            pg = ctx.new_page()
            resp = pg.goto(url, wait_until="domcontentloaded", timeout=30000)
            if resp is not None and resp.status >= 400:
                log("browser got HTTP", resp.status, url)
                b.close()
                return None
            try:
                pg.wait_for_load_state("networkidle", timeout=8000)
            except Exception:  # noqa: BLE001
                pass
            html = pg.content()
            b.close()
            log("browser rendered", url, len(html), "bytes")
            return html
    except Exception as e:  # noqa: BLE001
        log("playwright failed", url, type(e).__name__, str(e)[:150])
        return None


# --------------------------------------------------------------- search
def search(query, n=8):
    """Brave Search API if BRAVE_API_KEY is set, else free DuckDuckGo."""
    key = os.getenv("BRAVE_API_KEY")
    if key:
        try:
            r = requests.get(
                "https://api.search.brave.com/res/v1/web/search",
                headers={"X-Subscription-Token": key,
                         "Accept": "application/json"},
                params={"q": query, "count": n}, timeout=TIMEOUT)
            if r.ok:
                return [{"url": x["url"], "title": x.get("title", ""),
                         "snippet": x.get("description", "")}
                        for x in r.json().get("web", {}).get("results", [])]
        except Exception as e:  # noqa: BLE001
            log("brave failed", e)
    try:
        try:
            from ddgs import DDGS
        except ImportError:
            from duckduckgo_search import DDGS
        res = DDGS().text(query, max_results=n) or []
        return [{"url": x.get("href", ""), "title": x.get("title", ""),
                 "snippet": x.get("body", "")} for x in res if x.get("href")]
    except Exception as e:  # noqa: BLE001
        log("ddg failed", e)
        return []


def discover_sites(name, state, city, user_url):
    """Return candidate official sites, best first. User URL = one vote."""
    tokens = distinct(name)
    sites = {}

    def add(u, s):
        k = host_key(u)
        d = sites.setdefault(k, {"key": k, "origin": origin(u),
                                 "score": 0, "hints": []})
        d["score"] += s
        if u not in d["hints"]:
            d["hints"].append(u)

    if user_url:
        add(user_url if user_url.startswith("http") else "https://" + user_url, 3)
    loc = " ".join(x for x in (city, state) if x)
    queries = [
        f"{name} {loc} price transparency standard charges machine readable file",
        f"{name} {loc} hospital official website",
        f"{name} {loc}",
    ]
    for q in queries:
        for r in search(q):
            if any(b in host_key(r["url"]) for b in BAD_DOMAINS):
                continue
            text = f"{r['title']} {r['snippet']} {r['url']}".lower()
            add(r["url"], 1 + sum(1 for t in tokens if t in text))
    for d in sites.values():          # the hospital's own domain usually
        flat = d["key"].replace("-", "")   # contains a distinctive name word
        if any(len(t) >= 4 and t in flat for t in tokens):
            d["score"] += 4
    out = sorted(sites.values(), key=lambda d: -d["score"])
    log("sites:", [(d["key"], d["score"]) for d in out[:6]])
    return out


# ------------------------------------------------- cms-hpt.txt (free)
def read_cms_hpt(site_origin):
    p = urlparse(site_origin)
    host = p.netloc
    alt = host.removeprefix("www.") if host.startswith("www.") else "www." + host
    for h in (host, alt):
        base = f"{p.scheme}://{h}"
        r = get(base + "/cms-hpt.txt")
        if not r or "mrf-url" not in r.text.lower() \
                or r.text.lstrip().lower().startswith(("<!doctype", "<html")):
            continue
        entries, cur = [], {}
        for line in r.text.splitlines():
            if ":" not in line:
                continue
            k, v = line.split(":", 1)
            k, v = k.strip().lower(), v.strip()
            if k == "location-name" and cur:
                entries.append(cur)
                cur = {}
            cur[k] = v
        if cur:
            entries.append(cur)
        for e in entries:
            for k in ("mrf-url", "source-page-url"):
                if e.get(k):
                    e[k] = urljoin(base + "/", e[k])
        log("cms-hpt.txt at", base, len(entries), "entries")
        return entries
    return []


# ---------------------------------------------------------------- crawl
def extract_links(html, base):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for a in soup.find_all("a", href=True):
        h = urljoin(base, a["href"].strip())
        if h.startswith("http"):
            out.append((h, a.get_text(" ", strip=True)))
    return out


def page_links(url):
    if urlparse(url).path.lower().endswith(".pdf") or is_file(url):
        return []
    r = get(url)
    links = []
    if r and ("html" in r.headers.get("content-type", "").lower()
              or r.text.lstrip()[:1] == "<"):
        links = extract_links(r.text, r.url)
    has_file = any(is_file(h) or any(k in f"{h} {t}".lower() for k in FILE_HINTS)
                   for h, t in links)
    if not has_file:                       # maybe JavaScript-rendered
        html = render_html(url)
        if html:
            links += extract_links(html, url)
    return links


def score(url, text, tokens):
    low = f"{url} {text}".lower()
    s = 0
    if is_file(url):
        s += 3
    if any(h in low for h in FILE_HINTS):
        s += 3
    if re.search(r"(?<!\d)\d{9}(?!\d)", url):    # EIN in file name
        s += 2
    have = set(words(low))
    s += sum(1 for t in tokens if t in have)
    if ".pdf" in low:
        s -= 4
    if "/api/" in urlparse(url).path.lower() or "code=" in url.lower():
        s -= 10                              # dynamic aggregator endpoints
    if any(w in low for w in ("estimator", "shoppable", "faq", "policy",
                              "financial-assistance", "charity")):
        s -= 2
    return s


def crawl(pages, hospital, host):
    tokens = distinct(hospital)
    cands, subpages, done = {}, [], set()

    def collect(p, follow):
        for href, text in page_links(p):
            low = f"{href} {text}".lower()
            if (follow and not is_file(href) and host_key(href).endswith(host)
                    and any(h in low for h in PAGE_HINTS)):
                subpages.append(href)
            if is_bad_host(href):
                continue
            sc = score(href, text, tokens)
            if sc >= 3 and (is_file(href) or sc >= 6):
                cands[href] = max(sc, cands.get(href, 0))

    for p in pages[:6]:
        if p not in done:
            done.add(p)
            log("crawl", p)
            collect(p, True)
    for p in subpages:
        if len(done) >= 14:
            break
        if p not in done:
            done.add(p)
            log("crawl sub", p)
            collect(p, False)
    return [{"url": u, "score": s, "source": "crawl"} for u, s in cands.items()]


# --------------------------------------------------------------- verify
def peek(url, hospital):
    """Download only the first 128 KB and decide if it is this hospital's MRF."""
    r = fetch(url, headers={"Range": "bytes=0-131071"}, stream=True)
    if r is None:
        return {"ok": False, "error": "download failed or blocked"}
    try:
        chunk = b""
        for part in r.iter_content(32768):
            chunk += part
            if len(chunk) >= 131072:
                break
        cr = r.headers.get("content-range", "")
        info = {"ok": True, "final_url": str(r.url),
                "content_type": r.headers.get("content-type", ""),
                "size_bytes": cr.split("/")[-1] if "/" in cr
                else r.headers.get("content-length")}
    finally:
        try:
            r.close()
        except Exception:  # noqa: BLE001
            pass

    if not chunk or chunk.lstrip()[:15].lower().startswith((b"<!doctype", b"<html")):
        return {"ok": False, "error": "not a data file (HTML page)"}

    hints = FILE_HINTS + ("standard", "charge")
    if chunk[:2] == b"PK":                                   # zip / xlsx
        n = int.from_bytes(chunk[26:28], "little")
        inner = chunk[30:30 + n].decode("utf-8", "ignore")
        label = f"{inner} {urlparse(url).path}"
        if inner.startswith("[Content_Types]"):
            info.update(kind="xlsx",
                        name_score=name_match(hospital, urlparse(url).path),
                        covers=name_covers(hospital, urlparse(url).path))
        else:
            info.update(kind="zip", inner_file=inner,
                        name_score=name_match(hospital, label),
                        covers=name_covers(hospital, label))
        info["looks_like_mrf"] = any(h in label.lower() for h in hints)
        return info

    if chunk[:2] == b"\x1f\x8b":                             # gzip
        try:
            chunk = zlib.decompressobj(31).decompress(chunk)
        except zlib.error:
            pass
    text = chunk.decode("utf-8-sig", "ignore")
    low = text.lower()
    info.update(
        kind="json" if text.lstrip()[:1] in "{[" else "csv",
        looks_like_mrf=any(k in low for k in (
            "standard_charge", "hospital_name", "standard_charges",
            "last_updated_on", "charge_description")),
        name_score=name_match(hospital, low),
        covers=name_covers(hospital, low))
    return info


def evaluate(cands, hospital, limit=8):
    """Return ONLY candidates that are provably this hospital's MRF."""
    seen, out = set(), []
    for c in sorted(cands, key=lambda c: -c["score"]):
        if c["url"] in seen:
            continue
        seen.add(c["url"])
        if is_bad_host(c["url"]):
            continue
        if len(seen) > limit:
            break
        p = peek(c["url"], hospital)
        log("peek", c["url"], {k: p.get(k) for k in
                               ("ok", "kind", "looks_like_mrf", "name_score", "covers")})
        if not p.get("ok"):
            # Download blocked, but the link text/file name itself names the hospital
            path = urlparse(c["url"]).path
            if (is_file(c["url"]) and c["score"] >= 8
                    and name_covers(hospital, path)
                    and name_match(hospital, path) >= 0.8):
                out.append({**c, "ok": True, "kind": os.path.splitext(path)[1].lstrip("."),
                            "name_score": name_match(hospital, path),
                            "confidence": "medium",
                            "final_score": c["score"] + 8,
                            "unverified": "download blocked - matched by file name only"})
            else:
                log("REJECTED (could not download, name not provable):", c["url"])
            continue
        ns = p.get("name_score") or 0
        file_ok = p.get("looks_like_mrf") and p.get("covers") and ns >= 0.8
        list_ok = p.get("looks_like_mrf") and c.get("label_ok")   # cms-hpt says so
        if file_ok:
            conf = "high"
        elif list_ok:
            conf = "medium"
        else:
            log("REJECTED (file is not clearly this hospital's):", c["url"],
                "name_score=%.2f" % ns)
            continue
        out.append({**c, **p, "final_score": c["score"] + 10 * ns + 5,
                    "confidence": conf})
    return sorted(out, key=lambda x: -x["final_score"])


# ------------------------------------------------ last resort: Claude
def claude_fallback(name, state, city):
    key = os.getenv("ANTHROPIC_API_KEY")
    if not key:
        log("no ANTHROPIC_API_KEY set - Claude fallback skipped")
        return [], None
    prompt = (
        "Find the official CMS hospital price transparency machine-readable "
        "file (standard charges / chargemaster; .json, .csv or .zip) for: "
        f"{name}, {city} {state}. Use web search and only trust the hospital's "
        "own website or its health system - never third-party price-comparison "
        "sites. Reply ONLY with JSON: "
        '{"mrf_urls": ["direct file URLs"], "page_url": "price transparency '
        'page URL or null"}')
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01",
                     "content-type": "application/json"},
            json={"model": MODEL, "max_tokens": 1500,
                  "tools": [{"type": "web_search_20250305",
                             "name": "web_search", "max_uses": 5}],
                  "messages": [{"role": "user", "content": prompt}]},
            timeout=120)
        text = "".join(b.get("text", "") for b in r.json().get("content", [])
                       if b.get("type") == "text")
        data = json.loads(re.search(r"\{.*\}", text, re.S).group())
        return data.get("mrf_urls") or [], data.get("page_url")
    except Exception as e:  # noqa: BLE001
        log("claude fallback failed", e)
        return [], None


# ----------------------------------------------------------------- main
def find_mrf(name, state="", url="", city="", use_claude=True):
    res = {"hospital": name, "state": state, "mrf_url": None,
           "confidence": "none", "method": None, "alternatives": []}
    verified, tried = [], []

    for site in discover_sites(name, state, city, url)[:5]:
        tried.append(site["key"])
        pages = [site["origin"], *site["hints"]]
        cands = []
        for e in read_cms_hpt(site["origin"]):
            ns = name_match(name, e.get("location-name", ""))
            m = e.get("mrf-url", "")
            if m and is_file(m):
                cands.append({"url": m, "score": 10 + 10 * ns,
                              "source": "cms-hpt.txt",
                              "label_ok": name_covers(name, e.get("location-name", ""))})
            elif m:
                pages.append(m)
            if e.get("source-page-url"):
                pages.append(e["source-page-url"])
        cands += crawl(pages, name, site["key"])
        found = evaluate(cands, name)
        verified += found
        if found and found[0]["confidence"] == "high":
            break

    best = max(verified, key=lambda x: x["final_score"], default=None)
    if use_claude and (not best or best["confidence"] != "high"):
        log("falling back to Claude web search")
        urls, page = claude_fallback(name, state, city)
        cands = [{"url": u, "score": 8, "source": "claude-search"}
                 for u in urls if not is_bad_host(u)]
        if page:
            cands += crawl([page], name, host_key(page))
        verified += evaluate(cands, name)

    verified.sort(key=lambda x: -x["final_score"])
    if not verified:
        res["status"] = "not_found"
        blocked = sorted(k for k in tried if k in BLOCKED)
        res["note"] = (
            "No file could be proven to belong to this hospital, so no URL is "
            "returned. "
            + (f"These sites blocked automated access: {', '.join(blocked)}. "
               "This is often geo-blocking: retry from a US connection "
               "(--proxy) or set ANTHROPIC_API_KEY for the Claude search "
               "fallback. " if blocked else "")
            + "Run with -v to see why each candidate was rejected.")
    else:
        res["status"] = "found"
    if verified:
        b = verified[0]
        res.update(mrf_url=b["url"], confidence=b["confidence"],
                   method=b["source"], file_type=b.get("kind"),
                   size_bytes=b.get("size_bytes"),
                   name_match=round(b.get("name_score") or 0, 2),
                   verified_by_download="unverified" not in b)
        if b.get("unverified"):
            res["warning"] = b["unverified"]
        res["alternatives"] = [
            {"url": v["url"], "confidence": v["confidence"]}
            for v in verified[1:4]]
    return res


def safe_find(*a, **kw):
    try:
        return find_mrf(*a, **kw)
    except Exception as e:  # noqa: BLE001
        return {"hospital": a[0] if a else "", "mrf_url": None,
                "confidence": "none", "error": str(e)}


def main():
    global VERBOSE, VERIFY, HEADED, PROXY
    ap = argparse.ArgumentParser(description="Find a hospital MRF file URL")
    ap.add_argument("--name")
    ap.add_argument("--state", default="")
    ap.add_argument("--city", default="")
    ap.add_argument("--url", default="", help="optional, may be wrong")
    ap.add_argument("--csv", help="CSV with columns name,state,city,url")
    ap.add_argument("--out", help="output JSONL file for --csv (default stdout)")
    ap.add_argument("--workers", type=int, default=3)
    ap.add_argument("--no-claude", action="store_true",
                    help="never call the Claude fallback")
    ap.add_argument("--proxy", default="",
                    help="route site requests via a proxy, e.g. a US proxy for "
                         "sites that geo-block your country (or env MRF_PROXY)")
    ap.add_argument("--headed", action="store_true",
                    help="show the Playwright browser window")
    ap.add_argument("--insecure", action="store_true",
                    help="skip SSL certificate checks (corporate proxies)")
    ap.add_argument("-v", "--verbose", action="store_true")
    a = ap.parse_args()
    VERBOSE = a.verbose
    HEADED = a.headed
    if a.proxy:
        PROXY = a.proxy
    if a.insecure:
        VERIFY = False
        requests.packages.urllib3.disable_warnings()
    use_claude = not a.no_claude

    if a.csv:
        with open(a.csv, newline="", encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        out = open(a.out, "w", encoding="utf-8") if a.out else sys.stdout
        with ThreadPoolExecutor(a.workers) as ex:
            futs = [ex.submit(safe_find, r.get("name", ""), r.get("state", ""),
                              r.get("url", ""), r.get("city", ""), use_claude)
                    for r in rows if r.get("name")]
            for fut in as_completed(futs):
                out.write(json.dumps(fut.result()) + "\n")
                out.flush()
    elif a.name:
        print(json.dumps(safe_find(a.name, a.state, a.url, a.city, use_claude),
                         indent=2))
    else:
        ap.error("give --name, or --csv")


if __name__ == "__main__":
    main()
