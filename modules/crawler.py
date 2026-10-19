"""Polite, SSRF-safe HTTP fetching.

* every hop (including each redirect) is validated by safety.validate_url()
* robots.txt is always honoured for pages we read or submit to
* per-domain rate limiting shared by all threads
* bounded timeouts, response size and retry counts
"""
import threading
import time
import urllib.robotparser
from urllib.parse import urljoin, urlsplit

import requests
from bs4 import BeautifulSoup

from . import safety, settings

_rate_lock = threading.Lock()
_last_request = {}          # domain -> monotonic time of last reserved slot
_robots_cache = {}          # origin -> (expires_monotonic, parser_or_None, state)
_robots_lock = threading.Lock()
RETRY_STATUSES = (429, 500, 502, 503, 504)


class FetchResult:
    def __init__(self, url):
        self.url = url
        self.final_url = url
        self.status = None
        self.text = ""
        self.headers = {}
        self.redirects = []
        self.error = ""
        self.error_kind = ""   # timeout|connection|unsafe|robots|toolarge|nonhtml|stopped|ssl
        self.content_type = ""

    @property
    def ok(self):
        return not self.error and self.status is not None and 200 <= self.status < 400

    @property
    def temporary(self):
        return self.error_kind in ("timeout", "connection") or self.status in RETRY_STATUSES

    def as_dict(self):
        return {"url": self.url, "final_url": self.final_url, "status": self.status,
                "error": self.error, "error_kind": self.error_kind}


class Fetcher:
    def __init__(self, should_stop=None, cfg=None):
        self.cfg = cfg or settings.load()
        self.should_stop = should_stop or (lambda: False)
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": self.cfg["user_agent"],
            "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.5",
            "Accept-Language": "en;q=0.9",
        })
        self.ua_token = self.cfg["user_agent"].split("/")[0].split()[0]

    # ---- helpers -------------------------------------------------------
    def _sleep(self, seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if self.should_stop():
                return
            time.sleep(min(0.25, max(0.0, end - time.monotonic())))

    def _throttle(self, domain, extra_delay=0.0):
        delay = max(self.cfg["per_domain_delay"], extra_delay)
        with _rate_lock:
            now = time.monotonic()
            slot = max(now, _last_request.get(domain, 0) + delay)
            _last_request[domain] = slot
        wait = slot - time.monotonic()
        if wait > 0:
            self._sleep(wait)

    # ---- robots.txt ----------------------------------------------------
    def robots(self, url):
        """Return (allowed: bool, reason: str, crawl_delay: float)."""
        parts = urlsplit(url)
        origin = f"{parts.scheme}://{parts.netloc}"
        with _robots_lock:
            cached = _robots_cache.get(origin)
        if cached and cached[0] > time.monotonic():
            parser, state = cached[1], cached[2]
        else:
            parser, state = self._load_robots(origin)
        if state == "disallow_all":
            return False, "robots.txt is unavailable or forbids access", 0.0
        if parser is None:
            return True, "", 0.0
        allowed = parser.can_fetch(self.ua_token, url)
        delay = parser.crawl_delay(self.ua_token) or 0
        try:
            delay = min(float(delay), 10.0)
        except (TypeError, ValueError):
            delay = 0.0
        return allowed, ("" if allowed else "Disallowed by robots.txt"), delay

    def _load_robots(self, origin):
        res = self._once(origin + "/robots.txt", "GET", None, check_robots=False,
                         max_bytes=256 * 1024, allow_nonhtml=True)
        parser, state, ttl = None, "ok", 3600
        if res.error_kind == "unsafe":
            parser, state, ttl = None, "disallow_all", 300
        elif res.error:
            parser, state, ttl = None, "disallow_all", 300   # cannot read rules -> be conservative
        elif res.status in (401, 403) or (res.status and res.status >= 500):
            parser, state, ttl = None, "disallow_all", 300
        elif res.status == 200:
            parser = urllib.robotparser.RobotFileParser()
            parser.parse(res.text.splitlines())
        # other 4xx (e.g. 404) -> no robots.txt -> everything allowed
        with _robots_lock:
            _robots_cache[origin] = (time.monotonic() + ttl, parser, state)
        return parser, state

    # ---- core request --------------------------------------------------
    def fetch(self, url, method="GET", data=None, check_robots=True, max_bytes=None,
              allow_nonhtml=False, extra_headers=None):
        """GET/POST with bounded retries. Never raises for network problems."""
        res = None
        attempts = self.cfg["retry_limit"] + 1
        for attempt in range(attempts):
            if self.should_stop():
                res = FetchResult(url)
                res.error, res.error_kind = "Stopped before the request was made", "stopped"
                return res
            res = self._once(url, method, data, check_robots, max_bytes, allow_nonhtml, extra_headers)
            if not res.temporary or method.upper() != "GET":
                return res           # never blindly retry a POST
            if attempt < attempts - 1:
                wait = self.cfg["retry_delay"] * (2 ** attempt)
                if res.status == 429:
                    ra = res.headers.get("Retry-After", "")
                    if ra.isdigit():
                        wait = max(wait, min(int(ra), 30))
                self._sleep(wait)
        return res

    def _once(self, url, method, data, check_robots, max_bytes=None, allow_nonhtml=False,
              extra_headers=None):
        res = FetchResult(url)
        max_bytes = max_bytes or self.cfg["max_response_kb"] * 1024
        current, method = url, method.upper()
        timeout = (min(10, self.cfg["request_timeout"]), self.cfg["request_timeout"])
        try:
            for hop in range(self.cfg["max_redirects"] + 1):
                try:
                    current = safety.validate_url(current)
                except safety.UnsafeURL as exc:
                    res.error, res.error_kind = f"Blocked for safety: {exc}", "unsafe"
                    return res
                extra = 0.0
                if check_robots:
                    allowed, reason, extra = self.robots(current)
                    if not allowed:
                        res.error, res.error_kind = reason, "robots"
                        res.final_url = current
                        return res
                self._throttle(safety.get_domain(current), extra)
                if self.should_stop():
                    res.error, res.error_kind = "Stopped", "stopped"
                    return res
                resp = self.session.request(method, current, data=data if method == "POST" else None,
                                            headers=extra_headers or None, timeout=timeout,
                                            allow_redirects=False, stream=True)
                res.status, res.final_url = resp.status_code, current
                res.headers = {k: v for k, v in resp.headers.items()}
                res.content_type = resp.headers.get("Content-Type", "").lower()
                if resp.is_redirect or resp.status_code in (301, 302, 303, 307, 308):
                    loc = resp.headers.get("Location")
                    resp.close()
                    if not loc:
                        return res
                    if hop >= self.cfg["max_redirects"]:
                        res.error, res.error_kind = "Too many redirects", "connection"
                        return res
                    current = urljoin(current, loc)
                    res.redirects.append(current)
                    if resp.status_code in (301, 302, 303):
                        method, data = "GET", None
                    continue
                ctype = res.content_type
                textual = any(t in ctype for t in ("html", "xml", "text/", "json")) or not ctype
                if not textual and not allow_nonhtml:
                    resp.close()
                    res.error, res.error_kind = f"Not a web page ({ctype or 'unknown type'})", "nonhtml"
                    return res
                body = b""
                for chunk in resp.iter_content(chunk_size=16384):
                    body += chunk
                    if len(body) > max_bytes:
                        body = body[:max_bytes]
                        break
                resp.close()
                enc = resp.encoding or "utf-8"
                try:
                    res.text = body.decode(enc, errors="replace")
                except LookupError:
                    res.text = body.decode("utf-8", errors="replace")
                return res
        except requests.exceptions.Timeout:
            res.error, res.error_kind = "Request timed out", "timeout"
        except requests.exceptions.SSLError as exc:
            res.error, res.error_kind = f"TLS/SSL problem: {str(exc)[:160]}", "ssl"
        except requests.exceptions.RequestException as exc:
            res.error, res.error_kind = f"Connection problem: {str(exc)[:160]}", "connection"
        return res

    def probe(self, url):
        """Status code of a URL without downloading the page body. Returns (status|None, error)."""
        try:
            safe = safety.validate_url(url)
        except safety.UnsafeURL as exc:
            return None, f"Blocked: {exc}"
        allowed, reason, extra = self.robots(safe)
        if not allowed:
            return None, reason
        self._throttle(safety.get_domain(safe), extra)
        timeout = (min(10, self.cfg["request_timeout"]), self.cfg["request_timeout"])
        try:
            r = self.session.head(safe, timeout=timeout, allow_redirects=False)
            if r.status_code in (405, 501):
                r = self.session.get(safe, timeout=timeout, allow_redirects=False, stream=True)
                r.close()
            status = r.status_code
            hops = 0
            while status in (301, 302, 303, 307, 308) and hops < 3 and r.headers.get("Location"):
                nxt = safety.validate_url(urljoin(safe, r.headers["Location"]))
                self._throttle(safety.get_domain(nxt))
                r = self.session.head(nxt, timeout=timeout, allow_redirects=False)
                status, safe, hops = r.status_code, nxt, hops + 1
            return status, ""
        except safety.UnsafeURL as exc:
            return None, f"Blocked: {exc}"
        except requests.exceptions.RequestException as exc:
            return None, f"Connection problem: {str(exc)[:120]}"


# ---- HTML helpers used by several modules -------------------------------
def soup_of(html):
    return BeautifulSoup(html or "", "html.parser")


def page_title(soup):
    t = soup.find("title")
    return safety.clean_text(t.get_text(" ", strip=True), 200) if t else ""


def meta_description(soup):
    m = soup.find("meta", attrs={"name": lambda v: v and v.lower() == "description"})
    return safety.clean_text(m.get("content", ""), 400) if m else ""


def extract_links(soup, base_url):
    """All <a href> links as dicts: href(abs), text, rel(set)."""
    out = []
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "javascript:", "mailto:", "tel:", "data:")):
            continue
        absolute = urljoin(base_url, href)
        if not absolute.lower().startswith(("http://", "https://")):
            continue
        text = a.get_text(" ", strip=True)
        if not text:
            img = a.find("img", alt=True)
            text = img["alt"].strip() if img else ""
        rel = a.get("rel") or []
        if isinstance(rel, str):
            rel = rel.split()
        out.append({"href": absolute, "text": safety.clean_text(text, 200),
                    "rel": sorted({r.lower() for r in rel})})
    return out
