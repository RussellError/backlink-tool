"""Transparent quality assessment of a backlink opportunity.

Every factor is labelled "verified" (observed directly on the fetched page) or
"estimate" (a heuristic). No Domain Authority, Domain Rating, traffic or
index-count numbers are produced: those need third-party data we do not have.
TLDs such as .edu / .gov receive no bonus.
"""
import re
from urllib.parse import urlsplit

from . import crawler, db, safety, settings

STOPWORDS = set("""about above after again against all also and any are because been before being below between
both but can could did does doing down during each few for from further had has have having here how into its
just more most not now off once only other our out over own same should some such than that the their then
there these they this those through under until very was were what when where which while who whom why will with
would you your site website web page pages online best free new""".split())

SPAM_TERMS = ["casino", "viagra", "cialis", "payday loan", "porn", "escort", "adult dating", "replica watches",
              "online gambling", "sports betting", "essay writing service", "buy backlinks", "backlinks for sale",
              "cheap seo", "link farm", "buy followers", "forex signals", "weight loss pills", "cbd gummies",
              "crypto signals", "get rich quick"]
AUTOMATION_HINTS = ["no automated", "automated submission", "automatic submission", "auto-submit", "no bots",
                    "bots are not", "do not use bots", "submission software", "submitted by software",
                    "manual submissions only", "submit manually", "automated tools", "no scripts"]
CAPTCHA_RE = re.compile(r"recaptcha|g-recaptcha|hcaptcha|h-captcha|cf-turnstile|turnstile|arkoselabs|funcaptcha|"
                        r"geetest|captcha", re.I)
SUBMIT_LINK_RE = re.compile(r"submit (a |your )?(site|website|url|link|resource|tool|project|listing)|add (a |your )?"
                            r"(site|website|url|link|resource|tool|project|listing)|suggest (a |your )?(site|link|"
                            r"resource|tool|project)|get listed|list your|submit$|add listing|add$|"
                            r"write for us|guest post|become a contributor|contribute", re.I)
SUBMIT_HREF_RE = re.compile(r"submit|add[-_]?(site|url|link|listing|resource|tool)|suggest|get-listed|"
                            r"write-for-us|guest-post|contribute", re.I)
LOGIN_TEXT_RE = re.compile(r"log ?in to (submit|add|post|continue)|sign ?in to (submit|add|post|continue)|you must be "
                           r"(logged|signed) ?in|please (log|sign) ?in|login required|members only", re.I)
REGISTER_TEXT_RE = re.compile(r"create (an )?account to|register (to|an account)|sign ?up to (submit|add|list)|"
                              r"registration required|must (register|sign up)", re.I)
LOGIN_URL_RE = re.compile(r"/(log-?in|sign-?in|signin|auth|account/login|users?/login|session/new|sso)(/|$|\?)", re.I)
OUTREACH_CATEGORIES = ("guest_post", "industry_blog", "broken_link", "resource_page", "community_resource")


def tokens(text):
    words = re.findall(r"[a-z0-9][a-z0-9\-+#.]{2,}", (text or "").lower())
    return {w.strip(".-") for w in words if w.strip(".-") not in STOPWORDS and len(w.strip(".-")) > 2}


def analyze_page(html, url, target_domain=""):
    """Facts read directly from the HTML."""
    soup = crawler.soup_of(html)
    for tag in soup(["script", "style", "noscript", "template"]):
        tag.decompose()
    title = crawler.page_title(soup)
    desc = crawler.meta_description(soup)
    text = soup.get_text(" ", strip=True)
    lower = text.lower()
    headings = " ".join(h.get_text(" ", strip=True) for h in soup.find_all(["h1", "h2", "h3"])[:25])
    links = crawler.extract_links(soup, url)
    host = safety.get_domain(url)
    external = [l for l in links if safety.get_domain(l["href"]) not in ("", host)]
    flagged = [l for l in external if set(l["rel"]) & {"nofollow", "ugc", "sponsored"}]
    robots_meta = soup.find("meta", attrs={"name": lambda v: v and v.lower() in ("robots", "googlebot")})
    robots_val = (robots_meta.get("content", "") if robots_meta else "").lower()
    forms = soup.find_all("form")
    return {
        "soup": soup, "title": title, "description": desc, "text": text, "lower": lower, "headings": headings,
        "word_count": len(text.split()), "links": links, "external_links": external,
        "external_flagged": len(flagged), "noindex": "noindex" in robots_val,
        "has_password_field": bool(soup.find("input", attrs={"type": "password"})),
        "captcha": bool(CAPTCHA_RE.search(html or "")), "form_count": len(forms),
        "spam_hits": sorted({t for t in SPAM_TERMS if t in lower}),
        "automation_hints": sorted({h for h in AUTOMATION_HINTS if h in lower}),
        "lang": (soup.html.get("lang", "") if soup.html else "")[:12],
        "login_text": bool(LOGIN_TEXT_RE.search(text)), "register_text": bool(REGISTER_TEXT_RE.search(text)),
    }


def find_submission_link(info, url):
    host = safety.get_domain(url)
    best = None
    for l in info["links"]:
        if safety.get_domain(l["href"]) != host:
            continue
        label = l["text"].strip()
        if (label and SUBMIT_LINK_RE.search(label)) or SUBMIT_HREF_RE.search(urlsplit(l["href"]).path):
            best = best or l["href"]
            if label and SUBMIT_LINK_RE.search(label):
                return l["href"]
    return best


def page_has_submit_form(info):
    """A non-login, non-search form with a URL-like field."""
    for form in info["soup"].find_all("form"):
        if form.find("input", attrs={"type": "password"}):
            continue
        for inp in form.find_all("input"):
            blob = " ".join(str(inp.get(k, "")) for k in ("name", "id", "placeholder", "type")).lower()
            if re.search(r"\burl\b|website|site_?url|link|homepage|type=url", blob) or inp.get("type") == "url":
                return True
    return False


def relevance(target, info, url):
    """Keyword overlap estimate in 0..100, or None if the target has no keywords/description."""
    kw = tokens(target.get("keywords", "").replace(",", " "))
    other = tokens(" ".join([target.get("category", ""), target.get("name", ""), target.get("description", "")]))
    if not kw and not other:
        return None, []
    page = tokens(" ".join([info["title"], info["description"], info["headings"], info["lower"][:4000], url]))
    hits_kw, hits_other = sorted(kw & page), sorted((other - kw) & page)
    max_pts = len(kw) * 2 + min(len(other), 8)
    if max_pts == 0:
        return None, []
    pts = len(hits_kw) * 2 + min(len(hits_other), 8)
    score = min(100, int(round(100 * pts / min(max_pts, 14))))
    return score, (hits_kw + hits_other)[:10]


def dead_links(fetcher, info, page_url, limit):
    """Probe a few external links on a resource page. Verified HTTP statuses only."""
    dead, seen = [], set()
    for l in info["external_links"]:
        dom = safety.get_domain(l["href"])
        if dom in seen:
            continue
        seen.add(dom)
        if len(seen) > limit or fetcher.should_stop():
            break
        status, err = fetcher.probe(l["href"])
        if status in (404, 410):
            dead.append({"url": l["href"], "status": status, "anchor": l["text"]})
    return dead


def evaluate(opp, target, fetcher, cfg=None):
    """Assess one opportunity. Returns a dict that save_assessment() persists."""
    cfg = cfg or settings.load()
    verified, estimated, breakdown, flags = {}, {}, [], []
    reasons = []
    page_url = opp["url"]

    def add(factor, points, kind, note):
        breakdown.append({"factor": factor, "points": points, "kind": kind, "note": note})

    res = fetcher.fetch(page_url)
    verified["http_status"] = res.status
    verified["final_url"] = res.final_url
    out = {"score": 0, "classification": "review", "submit_url": opp.get("submit_url", ""),
           "http_status": res.status, "final_url": res.final_url, "category": opp["category"],
           "notes": opp.get("notes", ""), "title": opp.get("title", "")}

    # ---- not reachable / not readable ----
    if res.error_kind == "robots":
        flags.append("robots.txt does not allow automated inspection of this page")
        verified["inspection"] = "blocked by robots.txt"
        out.update(classification="review", summary="Could not inspect: blocked by robots.txt. Review manually.")
        return _finish(out, verified, estimated, breakdown, flags, opp, "review")
    if res.error or res.status is None:
        verified["fetch_error"] = res.error
        temp = res.temporary
        out.update(classification="review" if temp else "rejected",
                   summary=("Temporarily unreachable: " if temp else "Not reachable: ") + (res.error or "no response"))
        flags.append(out["summary"])
        out["last_error"] = res.error
        return _finish(out, verified, estimated, breakdown, flags, opp, out["classification"])
    if res.status in (401, 403):
        verified["access"] = f"HTTP {res.status} (restricted)"
        out.update(classification="login", summary=f"Access restricted (HTTP {res.status}); likely needs login.")
        flags.append("Access restricted")
        return _finish(out, verified, estimated, breakdown, flags, opp, "login")
    if res.status in (404, 410) or res.status >= 400:
        temp = res.status in (429, 500, 502, 503, 504)
        out.update(classification="review" if temp else "rejected",
                   summary=f"Page returned HTTP {res.status}.", last_error=f"HTTP {res.status}")
        flags.append(out["summary"])
        add("Page accessible", 0, "verified", f"HTTP {res.status}")
        return _finish(out, verified, estimated, breakdown, flags, opp, out["classification"])

    # ---- readable page ----
    info = analyze_page(res.text, res.final_url, target.get("domain", ""))
    score = 0
    add("Page accessible", 20, "verified", f"HTTP {res.status}")
    score += 20
    if res.final_url.startswith("https://"):
        add("HTTPS", 5, "verified", "Served over HTTPS")
        score += 5
    verified.update(title=info["title"], meta_description=info["description"], word_count=info["word_count"],
                    external_links=len(info["external_links"]), external_links_flagged=info["external_flagged"],
                    noindex_meta=info["noindex"], captcha_detected=info["captcha"],
                    password_field=info["has_password_field"], forms=info["form_count"], language=info["lang"])
    out["title"] = out["title"] or info["title"]

    if info["noindex"]:
        flags.append("Page has a noindex robots meta tag")
        add("Indexable", -100, "verified", "noindex meta tag found")
        score -= 100
    else:
        verified["indexing"] = "No noindex tag seen. Whether the page is actually in a search index cannot be verified here."
        add("Indexable", 5, "verified", "No noindex meta tag (actual index status is unknown)")
        score += 5
    if info["word_count"] >= 300:
        add("Content depth", 10, "verified", f"{info['word_count']} words of text"); score += 10
    elif info["word_count"] >= 100:
        add("Content depth", 5, "verified", f"{info['word_count']} words of text"); score += 5
    else:
        flags.append("Very little text on the page (JavaScript-rendered or thin content?)")
        add("Content depth", 0, "verified", f"Only {info['word_count']} words")
    if info["title"] and info["description"]:
        add("Title and meta description", 5, "verified", "Both present"); score += 5

    rel, matched = relevance(target, info, res.final_url)
    if rel is None:
        estimated["topical_relevance"] = "not assessed (add keywords or a description to the website profile)"
        add("Topical relevance", 0, "estimate", "Target has no keywords/description")
    else:
        estimated["topical_relevance"] = rel
        estimated["matched_terms"] = matched
        pts = int(round(rel * 0.30))
        add("Topical relevance", pts, "estimate", f"Keyword overlap {rel}/100; matched: {', '.join(matched) or 'none'}")
        score += pts
        if rel < 10:
            flags.append("Little topical overlap with your site")

    ext = len(info["external_links"])
    if ext == 0:
        flags.append("No external links seen on the page; external links may not be allowed")
        add("External links present", 0, "verified", "None observed")
    elif info["external_flagged"] < ext:
        add("External links present", 10, "verified",
            f"{ext} external links, {info['external_flagged']} marked nofollow/ugc/sponsored"); score += 10
    else:
        add("External links present", 3, "verified", f"All {ext} external links are marked nofollow/ugc/sponsored")
        score += 3
    if ext > 150:
        flags.append(f"Link-heavy page ({ext} external links)")
        add("Link density", -10, "verified", f"{ext} external links"); score -= 10

    if info["spam_hits"]:
        pen = min(40, 10 * len(info["spam_hits"]))
        add("Spam signals", -pen, "verified", "Found: " + ", ".join(info["spam_hits"])); score -= pen
        flags.append("Spam-related terms: " + ", ".join(info["spam_hits"]))
    verified["spam_terms"] = info["spam_hits"]

    submit_form = page_has_submit_form(info)
    sub_link = None if submit_form else find_submission_link(info, res.final_url)
    submit_url = res.final_url if submit_form else (sub_link or "")
    sub_info = None
    if sub_link and not fetcher.should_stop():       # inspect the separate submission page too
        sres = fetcher.fetch(sub_link)
        if sres.ok:
            sub_info = analyze_page(sres.text, sres.final_url)
            submit_url = sres.final_url
            submit_form = page_has_submit_form(sub_info)
            if sub_info["captcha"]:
                info["captcha"] = True
            if sub_info["has_password_field"] or sub_info["login_text"]:
                info["login_text"] = True
            info["register_text"] = info["register_text"] or sub_info["register_text"]
            info["automation_hints"] = sorted(set(info["automation_hints"]) | set(sub_info["automation_hints"]))
            verified["submission_page_status"] = sres.status
        else:
            verified["submission_page_status"] = sres.error or f"HTTP {sres.status}"
            flags.append("Submission page could not be opened: " + (sres.error or f"HTTP {sres.status}"))
            submit_url = sub_link
    verified["submission_page"] = submit_url or "none found"
    if submit_url:
        add("Submission page exists", 10, "verified", submit_url); score += 10
        out["submit_url"] = submit_url
    login = (res.status in (401, 403) or info["has_password_field"] or info["login_text"]
             or bool(LOGIN_URL_RE.search(urlsplit(res.final_url).path)))
    register = info["register_text"]
    verified["login_required"] = bool(login)
    verified["registration_required"] = bool(register)
    if not (login or register or info["captcha"]):
        add("No login/CAPTCHA seen", 5, "verified", "Neither detected on this page"); score += 5
    if info["captcha"]:
        flags.append("CAPTCHA detected (will not be bypassed)")
    if login:
        flags.append("Login appears to be required")
    if register:
        flags.append("Registration appears to be required")
    if info["automation_hints"]:
        flags.append("Page mentions restrictions on automated submissions: " + "; ".join(info["automation_hints"]))
        verified["automation_restriction_text"] = info["automation_hints"]

    # redirected away from the host we asked about?
    if safety.get_domain(res.final_url) != safety.get_domain(page_url):
        flags.append(f"Redirected to a different domain: {safety.get_domain(res.final_url)}")

    # broken-link scan on resource pages
    if cfg["broken_link_scan"] and opp["category"] in ("resource_page", "community_resource") \
            and cfg["broken_link_max_checks"] > 0 and not info["noindex"]:
        dl = dead_links(fetcher, info, res.final_url, cfg["broken_link_max_checks"])
        verified["dead_links_checked"] = min(len({safety.get_domain(l['href']) for l in info['external_links']}),
                                             cfg["broken_link_max_checks"])
        if dl:
            verified["dead_links"] = dl
            out["category"] = "broken_link"
            out["notes"] = ("Dead links found (HTTP 404/410): " +
                            "; ".join(f"{d['url']} ({d['status']})" for d in dl[:5]))
            flags.append(f"{len(dl)} dead link(s) found: a replacement could be suggested politely")

    score = max(0, min(100, score))
    out["score"] = score

    # ---- classification ----
    cls = "review"
    if info["noindex"] or len(info["spam_hits"]) >= 3:
        cls, why = "rejected", "noindex or spam-heavy"
    elif info["captcha"]:
        cls, why = "captcha", "CAPTCHA present"
    elif login or register:
        cls, why = "login", "login/registration required"
    elif out["category"] in OUTREACH_CATEGORIES:
        cls, why = "outreach", "needs a personal approach (contact the site owner)"
    elif submit_url and score >= cfg["min_eligible_score"] and not info["automation_hints"] \
            and submit_form:
        cls, why = "eligible", "submission form found; still checked again before any submit"
    else:
        cls, why = "review", "not enough evidence for automation; check manually"
    if cls in ("review",) and opp.get("hint_status") in ("login", "captcha", "outreach"):
        cls, why = opp["hint_status"], "known requirement for this kind of site"
    out["summary"] = f"Score {score}/100 (heuristic). Class: {cls} - {why}."
    out["classification"] = cls
    return _finish(out, verified, estimated, breakdown, flags, opp, cls)


def _finish(out, verified, estimated, breakdown, flags, opp, cls):
    out.update(verified=verified, estimated=estimated, breakdown=breakdown, flags=flags)
    out.setdefault("summary", "")
    out["classification"] = cls
    return out


def save_assessment(opp, result):
    """Persist assessment + update the opportunity (unless the user set the status by hand)."""
    ts = db.now()
    with db.transaction() as conn:
        conn.execute("""INSERT INTO assessments (opportunity_id, score, summary, verified, estimated, breakdown,
                         flags, http_status, final_url, assessed_at) VALUES (?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(opportunity_id) DO UPDATE SET score=excluded.score, summary=excluded.summary,
                         verified=excluded.verified, estimated=excluded.estimated, breakdown=excluded.breakdown,
                         flags=excluded.flags, http_status=excluded.http_status, final_url=excluded.final_url,
                         assessed_at=excluded.assessed_at""",
                     (opp["id"], result["score"], result["summary"], db.dumps(result["verified"]),
                      db.dumps(result["estimated"]), db.dumps(result["breakdown"]), db.dumps(result["flags"]),
                      result.get("http_status"), result.get("final_url", ""), ts))
        row = conn.execute("SELECT user_override FROM opportunities WHERE id=?", (opp["id"],)).fetchone()
        override = bool(row and row["user_override"])
        status_sql = "" if override else "status=?, "
        params = [] if override else [result["classification"]]
        conn.execute(f"""UPDATE opportunities SET {status_sql}category=?, notes=?, title=?, submit_url=?,
                         last_error=?, updated_at=? WHERE id=?""",
                     params + [result.get("category", opp["category"]), result.get("notes", opp.get("notes", "")),
                               result.get("title") or opp.get("title", ""), result.get("submit_url", ""),
                               result.get("last_error", ""), ts, opp["id"]])
