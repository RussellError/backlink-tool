"""Opportunity discovery.

Sources that are actually implemented:
  1. seed       - data/sources.json starter list (offline; assessed later)
  2. github     - GitHub's public search API for 'awesome'-style lists (no key needed, rate limited)
  3. searxng    - a SearXNG instance YOU configure (JSON API); no instance = step skipped
  4. reference  - pages you paste in: the page itself plus its outbound links become candidates
  5. import/manual - handled by import_urls() and add_opportunity()

There is no pretence of searching "the whole internet": without a configured
search source, discovery only covers 1, 2, 4 and your own imports.
"""
import csv
import io
import json
import os
import re
from urllib.parse import quote_plus

from . import crawler, db, safety, settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(BASE_DIR, "data", "sources.json")

CATEGORIES = {
    "directory": "Relevant web directory",
    "profile": "Business or creator profile",
    "open_source": "Open-source project page",
    "github_docs": "GitHub / GitLab project documentation",
    "resource_page": "Relevant resource page",
    "industry_blog": "Industry blog accepting contributions",
    "guest_post": "Guest-post opportunity",
    "broken_link": "Broken-link replacement",
    "community_resource": "Community resource page",
    "submission_site": "Website offering legitimate submissions",
}
# search templates per category for SearXNG
QUERY_TEMPLATES = {
    "directory": '"{kw}" directory submit site',
    "resource_page": '"{kw}" resources useful links',
    "guest_post": '"{kw}" "write for us"',
    "industry_blog": '"{kw}" blog contribute article',
    "community_resource": '"{kw}" community resources list',
    "submission_site": '"{kw}" "submit your" tool OR site',
}


def load_seed_sources():
    try:
        with open(SOURCES_PATH, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("sources", [])
    except FileNotFoundError:
        return []
    except json.JSONDecodeError as exc:
        db.log("ERROR", "finder", "data/sources.json is not valid JSON", str(exc))
        return []


def add_opportunity(target_id, url, category, source="manual", title="", notes="", hint_status="",
                    submit_url=""):
    """Insert if new. Returns (opportunity_id, created). Raises safety.UnsafeURL on bad input."""
    if category not in CATEGORIES:
        raise ValueError(f"Unknown category: {category}")
    clean = safety.normalize_url(safety.validate_url(safety.normalize_user_url(url), resolve=False))
    target = db.query("SELECT domain FROM targets WHERE id=?", (target_id,), one=True)
    if not target:
        raise ValueError("Unknown target website")
    domain = safety.get_domain(clean)
    if domain == target["domain"]:
        raise safety.UnsafeURL("That page is on your own website")
    ts = db.now()
    res = db.execute("""INSERT OR IGNORE INTO opportunities
        (target_id, url, domain, category, source, title, notes, hint_status, submit_url, discovered_at, updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (target_id, clean, domain, category, source[:80], safety.clean_text(title, 200),
                      safety.clean_text(notes, 600), hint_status, submit_url, ts, ts))
    if res.rowcount:
        return res.lastrowid, True
    row = db.query("SELECT id FROM opportunities WHERE target_id=? AND url=?", (target_id, clean), one=True)
    return (row["id"] if row else None), False


def import_urls(target_id, filename, raw_bytes, default_category):
    """Import from .txt (one URL per line) or .csv (url[,category[,title[,notes]]]; header optional)."""
    if default_category not in CATEGORIES:
        raise ValueError("Choose a valid opportunity category")
    if len(raw_bytes) > 2 * 1024 * 1024:
        raise ValueError("File is larger than 2 MB")
    text = raw_bytes.decode("utf-8-sig", errors="replace")
    rows = []
    if filename.lower().endswith(".csv"):
        for rec in csv.reader(io.StringIO(text)):
            if not rec or not rec[0].strip():
                continue
            rows.append([c.strip() for c in rec] + [""] * 4)
    else:
        for line in text.splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                rows.append([line.split()[0], "", "", ""])
    stats = {"added": 0, "duplicates": 0, "invalid": [], "total": 0}
    for rec in rows[:5000]:
        url, cat, title, notes = rec[0], rec[1], rec[2], rec[3]
        if rec is rows[0] and url.lower() in ("url", "link", "website", "opportunity_url"):
            continue
        stats["total"] += 1
        category = cat if cat in CATEGORIES else default_category
        try:
            _, created = add_opportunity(target_id, url, category, source="import:" + os.path.basename(filename)[:40],
                                         title=title, notes=notes)
            stats["added" if created else "duplicates"] += 1
        except (safety.UnsafeURL, ValueError) as exc:
            if len(stats["invalid"]) < 25:
                stats["invalid"].append({"value": url[:120], "reason": str(exc)})
    db.log("INFO", "import", f"Imported {stats['added']} new, {stats['duplicates']} duplicates, "
           f"{len(stats['invalid'])} invalid from {filename}", target_id=target_id)
    return stats


# ---------------------------------------------------------------- discovery
def keywords_of(target):
    kws = [k.strip() for k in (target.get("keywords") or "").split(",") if k.strip()]
    return kws or ([target["category"]] if target.get("category") else [])


def build_steps(target, params, cfg):
    """Deterministic list of discovery steps; stored in the job checkpoint so a restart resumes exactly."""
    wanted = [t for t in (target.get("opp_types") or "").split(",") if t in CATEGORIES]
    steps = []
    if cfg["enable_seed_sources"]:
        for s in load_seed_sources():
            if not wanted or s.get("category") in wanted:
                steps.append({"type": "seed", "name": s.get("name", ""), "url": s.get("url", ""),
                              "category": s.get("category", "directory"), "hint": s.get("hint_status", ""),
                              "notes": s.get("notes", "")})
    kws = keywords_of(target)[:4]
    if cfg["enable_github_search"]:
        for kw in kws:
            steps.append({"type": "github", "query": kw})
    if cfg["searxng_url"] and cfg["searxng_max_queries"] > 0:
        cats = [c for c in QUERY_TEMPLATES if not wanted or c in wanted]
        queries = [{"type": "searxng", "category": c, "query": QUERY_TEMPLATES[c].format(kw=kw)}
                   for kw in kws for c in cats]
        steps.extend(queries[:cfg["searxng_max_queries"]])
    for ref in (params.get("reference_urls") or [])[:10]:
        steps.append({"type": "reference", "url": ref})
    return steps


def run_step(step, target, fetcher, cfg, ctx=None):
    """Execute one step. Returns number of new opportunities saved."""
    kind, added = step["type"], 0
    tid = target["id"]
    if kind == "seed":
        try:
            _, created = add_opportunity(tid, step["url"], step["category"], source="seed", title=step["name"],
                                         notes=step["notes"], hint_status=step["hint"])
            added += int(created)
        except (safety.UnsafeURL, ValueError) as exc:
            db.log("WARNING", "finder", f"Seed skipped: {step.get('url')}", str(exc), tid)
    elif kind == "github":
        added = _github(step["query"], target, fetcher)
    elif kind == "searxng":
        added = _searxng(step, target, fetcher, cfg)
    elif kind == "reference":
        added = _reference(step["url"], target, fetcher, cfg)
    db.log("INFO", "finder", f"Step {kind} finished: {added} new", json.dumps(step)[:300], tid)
    return added


def _github(query, target, fetcher):
    """GitHub public search API (documented, rate limited: ~10 requests/minute unauthenticated)."""
    q = quote_plus(f"{query} awesome in:name,description,topics archived:false")
    headers = {"Accept": "application/vnd.github+json"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"
    res = fetcher.fetch(f"https://api.github.com/search/repositories?q={q}&sort=stars&per_page=10",
                        check_robots=False, allow_nonhtml=True, extra_headers=headers)
    if res.status in (403, 429):
        db.log("WARNING", "finder", "GitHub API rate limit reached; set GITHUB_TOKEN or try later", "", target["id"])
        return 0
    if not res.ok:
        db.log("WARNING", "finder", "GitHub search failed", res.error or f"HTTP {res.status}", target["id"])
        return 0
    try:
        items = json.loads(res.text).get("items", [])
    except ValueError:
        db.log("WARNING", "finder", "GitHub search returned unreadable data", "", target["id"])
        return 0
    added = 0
    for it in items:
        if it.get("archived") or not it.get("html_url"):
            continue
        note = (f"Awesome-style list, {it.get('stargazers_count', 0)} stars, last push {str(it.get('pushed_at', ''))[:10]}. "
                "Contributions are made by pull request from your own GitHub account; follow the CONTRIBUTING rules.")
        try:
            _, created = add_opportunity(target["id"], it["html_url"], "github_docs", source="github-search",
                                         title=it.get("full_name", ""), notes=note, hint_status="login")
            added += int(created)
        except (safety.UnsafeURL, ValueError):
            continue
    return added


def _searxng(step, target, fetcher, cfg):
    base = cfg["searxng_url"].rstrip("/")
    res = fetcher.fetch(f"{base}/search?q={quote_plus(step['query'])}&format=json", check_robots=False,
                        allow_nonhtml=True)
    if not res.ok:
        db.log("WARNING", "finder", "SearXNG query failed", res.error or f"HTTP {res.status}", target["id"])
        return 0
    try:
        results = json.loads(res.text).get("results", [])
    except ValueError:
        db.log("WARNING", "finder", "SearXNG did not return JSON (is format=json enabled on the instance?)", "",
               target["id"])
        return 0
    added = 0
    for r in results[:15]:
        try:
            _, created = add_opportunity(target["id"], r.get("url", ""), step["category"], source="searxng",
                                         title=r.get("title", ""), notes=safety.clean_text(r.get("content", ""), 300))
            added += int(created)
        except (safety.UnsafeURL, ValueError):
            continue
    return added


def _reference(url, target, fetcher, cfg):
    try:
        clean = safety.validate_url(safety.normalize_user_url(url))
    except safety.UnsafeURL as exc:
        db.log("WARNING", "finder", f"Reference URL rejected: {url}", str(exc), target["id"])
        return 0
    res = fetcher.fetch(clean)
    if not res.ok:
        db.log("WARNING", "finder", f"Reference page not readable: {clean}", res.error or f"HTTP {res.status}",
               target["id"])
        return 0
    soup = crawler.soup_of(res.text)
    added = 0
    try:
        _, created = add_opportunity(target["id"], res.final_url, "resource_page", source="reference",
                                     title=crawler.page_title(soup))
        added += int(created)
    except (safety.UnsafeURL, ValueError):
        pass
    seen = set()
    for l in crawler.extract_links(soup, res.final_url):
        dom = safety.get_domain(l["href"])
        if not dom or dom in seen or dom == safety.get_domain(res.final_url):
            continue
        seen.add(dom)
        if len(seen) > cfg["max_candidates_per_run"]:
            break
        try:
            _, created = add_opportunity(target["id"], l["href"], "resource_page",
                                         source="reference:" + safety.get_domain(res.final_url),
                                         title=l["text"])
            added += int(created)
        except (safety.UnsafeURL, ValueError):
            continue
    return added
