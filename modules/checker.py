"""Backlink verification: does the source page really link to the target?"""
from urllib.parse import urlsplit

from . import crawler, db, safety, settings

LIVE_BY_REL = (("sponsored", "live_sponsored"), ("ugc", "live_ugc"), ("nofollow", "live_nofollow"))


def _matches(href, target_domain):
    host = safety.get_domain(href)
    return bool(host) and (host == target_domain or host.endswith("." + target_domain))


def check_backlink(fetcher, source_url, target_url):
    """Return a dict describing what is actually on the source page right now."""
    target_domain = safety.get_domain(target_url)
    out = {"state": "inconclusive", "final_url": "", "http_status": None, "anchor_text": "", "rel": "",
           "href": "", "link_count": 0, "error": ""}
    res = fetcher.fetch(source_url)
    out["final_url"], out["http_status"] = res.final_url, res.status
    if res.error_kind == "robots":
        out["error"] = "Not checked: robots.txt does not allow automated access"
        return out
    if res.error_kind == "unsafe":
        out["error"] = res.error
        return out
    if res.error_kind in ("timeout", "connection", "ssl"):
        out.update(state="temporary_failure", error=res.error)
        return out
    if res.error_kind == "nonhtml":
        out["error"] = res.error
        return out
    if res.status in (404, 410):
        out.update(state="not_found", error=f"Source page returned HTTP {res.status}")
        return out
    if res.status in (401, 403):
        out.update(state="access_restricted", error=f"HTTP {res.status}: access restricted")
        return out
    if res.status in (429, 500, 502, 503, 504):
        out.update(state="temporary_failure", error=f"HTTP {res.status}")
        return out
    if res.status is None or res.status >= 400:
        out["error"] = res.error or f"HTTP {res.status}"
        return out

    soup = crawler.soup_of(res.text)
    links = [l for l in crawler.extract_links(soup, res.final_url) if _matches(l["href"], target_domain)]
    out["link_count"] = len(links)
    moved = (safety.normalize_url(res.final_url) != safety.normalize_url(source_url)
             and (safety.get_domain(res.final_url) != safety.get_domain(source_url)
                  or urlsplit(res.final_url).path.rstrip("/") != urlsplit(source_url).path.rstrip("/")))
    if links:
        best = next((l for l in links if not set(l["rel"]) & {"nofollow", "ugc", "sponsored"}), links[0])
        out.update(href=best["href"], anchor_text=best["text"] or "(no text)", rel=",".join(best["rel"]))
        state = "live"
        for token, label in LIVE_BY_REL:
            if token in best["rel"]:
                state = label
                break
        out["state"] = state
        if moved:
            out["error"] = f"Source URL redirects to {res.final_url}; link found there"
        return out
    if moved:
        out.update(state="redirected", error=f"Source page redirects to {res.final_url}; no link to the target found there")
        return out
    # Page loaded fine but no link. JS-rendered pages cannot be judged from raw HTML.
    scripts = len(BeautifulSoupScripts(res.text))
    words = len(soup.get_text(" ", strip=True).split())
    if words < 60 and scripts >= 3:
        out.update(state="inconclusive", error="Page looks JavaScript-rendered; link may exist but is not in the raw HTML")
        return out
    out.update(state="not_found", error="Page loaded but contains no link to the target")
    return out


def BeautifulSoupScripts(html):
    return crawler.soup_of(html).find_all("script")


def run_check(backlink_id, fetcher, cfg=None):
    """Check one saved backlink, store the result and history, schedule the next check."""
    cfg = cfg or settings.load()
    bl = db.query("SELECT b.*, t.url AS target_url FROM backlinks b JOIN targets t ON t.id=b.target_id WHERE b.id=?",
                  (backlink_id,), one=True)
    if not bl:
        raise ValueError("Backlink not found")
    r = check_backlink(fetcher, bl["source_url"], bl["target_url"])
    ts = db.now()
    live = r["state"] in db.LIVE_STATES
    first_seen = bl["first_seen"] or (ts if live else None)
    was_live = bl["state"] in db.LIVE_STATES
    disappeared = 1 if (was_live and r["state"] == "not_found") else (0 if live else bl["disappeared"])
    failures = bl["consecutive_failures"] + 1 if r["state"] in ("temporary_failure", "inconclusive") else 0
    if r["state"] == "temporary_failure":
        nxt = db.hours_from_now(min(6 * failures, 48))
    elif live:
        nxt = db.hours_from_now(cfg["recheck_interval_hours"])
    else:
        nxt = db.hours_from_now(cfg["recheck_interval_hours"])
    # A transient failure must not erase what we last knew about a live link.
    keep_prev = r["state"] == "temporary_failure" and was_live
    with db.transaction() as conn:
        conn.execute("""UPDATE backlinks SET state=?, final_url=?, http_status=?, href=?, anchor_text=?, rel=?,
                        link_count=?, first_seen=?, last_checked=?, next_check=?, check_count=check_count+1,
                        consecutive_failures=?, disappeared=?, error=? WHERE id=?""",
                     (bl["state"] if keep_prev else r["state"], r["final_url"], r["http_status"],
                      r["href"] or (bl["href"] if keep_prev else ""),
                      r["anchor_text"] or (bl["anchor_text"] if keep_prev else ""),
                      r["rel"] if not keep_prev else bl["rel"], r["link_count"], first_seen, ts, nxt, failures,
                      disappeared, r["error"], backlink_id))
        conn.execute("""INSERT INTO verification_history (backlink_id, checked_at, state, http_status, final_url,
                        anchor_text, rel, error) VALUES (?,?,?,?,?,?,?,?)""",
                     (backlink_id, ts, r["state"], r["http_status"], r["final_url"], r["anchor_text"], r["rel"],
                      r["error"]))
    if disappeared and r["state"] == "not_found":
        db.log("WARNING", "checker", f"Link disappeared: {bl['source_url']}", "", bl["target_id"])
    else:
        db.log("INFO", "checker", f"{bl['source_url']} -> {r['state']}", r["error"], bl["target_id"])
    return r


def add_backlink(target_id, source_url):
    """Add a page to monitor (e.g. a listing you know about). Returns (id, created)."""
    clean = safety.normalize_url(safety.validate_url(safety.normalize_user_url(source_url), resolve=False))
    target = db.query("SELECT domain FROM targets WHERE id=?", (target_id,), one=True)
    if not target:
        raise ValueError("Unknown target website")
    if safety.get_domain(clean) == target["domain"]:
        raise safety.UnsafeURL("Source pages on your own website do not count as backlinks")
    res = db.execute("INSERT OR IGNORE INTO backlinks (target_id, source_url, state, next_check, created_at) "
                     "VALUES (?,?, 'pending', ?, ?)", (target_id, clean, db.now(), db.now()))
    if res.rowcount:
        return res.lastrowid, True
    row = db.query("SELECT id FROM backlinks WHERE target_id=? AND source_url=?", (target_id, clean), one=True)
    return row["id"], False


def due_ids(target_id, force=False):
    if force:
        rows = db.query("SELECT id FROM backlinks WHERE target_id=? ORDER BY id", (target_id,))
    else:
        rows = db.query("SELECT id FROM backlinks WHERE target_id=? AND (next_check IS NULL OR next_check<=?) "
                        "ORDER BY id", (target_id, db.now()))
    return [r["id"] for r in rows]
