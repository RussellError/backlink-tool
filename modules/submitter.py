"""Permitted-only submission engine.

A submission is attempted automatically ONLY when every check passes:
  profile allows it, robots.txt allows the form path, no CAPTCHA, no login,
  no automation-prohibiting text, the form posts to the same site, every
  required field can be filled from your profile, no consent checkbox is
  required, and (by default) you approved the opportunity.
Otherwise the prepared details + a direct link are returned for manual work.

A successful HTTP response is never reported as a live backlink: the best
outcome is "submitted" / "awaiting approval", and the backlink is recorded
as "pending" until the verifier actually finds the link.
"""
import json
import os
import re
from urllib.parse import urljoin

from . import crawler, db, quality, safety, settings

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROFILES_PATH = os.path.join(BASE_DIR, "data", "submissions.json")
DONE_STATUSES = ("submitted", "awaiting_approval", "pending_verification", "already_submitted")

URL_RE = re.compile(r"(^|[_\-\s\[\]])(url|website|web[_\-\s]?site|site[_\-\s]?url|link|homepage|web[_\-\s]?address|domain)([_\-\s\[\]]|$)", re.I)
NAME_RE = re.compile(r"(title|site[_\-\s]?name|business|company|project|your[_\-\s]?name|contact[_\-\s]?name|^name$|[_\-\s]name$|^name[_\-\s])", re.I)
DESC_RE = re.compile(r"(descr|about|summary|details|abstract|tagline|short[_\-\s]?desc)", re.I)
EMAIL_RE = re.compile(r"e[\-_]?mail", re.I)
KEYW_RE = re.compile(r"(keyword|tags?\b)", re.I)
CATEGORY_RE = re.compile(r"(categor|topic|section|type\b)", re.I)
LANG_RE = re.compile(r"(lang)", re.I)
COUNTRY_RE = re.compile(r"(country|region|location)", re.I)
HONEYPOT_RE = re.compile(r"honey|\bhp\b|_hp|hp_|trap|bot[_\-]?field|leave[_\-]?blank|do[_\-]?not[_\-]?fill|nospam|confirm[_\-]?url2", re.I)
CONSENT_RE = re.compile(r"(terms|privacy|agree|accept|consent|policy|gdpr|conditions)", re.I)
SEARCH_NAMES = {"q", "s", "search", "query", "keyword", "keywords", "k", "term"}
COMMENT_FIELDS = {"comment", "author", "comment_post_id", "comment_parent"}
SUCCESS_RE = re.compile(r"thank you|thanks for (your )?submi|successfully submitted|has been submitted|"
                        r"submission (was )?(received|successful)|we have received|been received|submitted successfully", re.I)
AWAIT_RE = re.compile(r"pending|awaiting|moderat|will be reviewed|being reviewed|under review|for review|approval|"
                      r"approved by|editor will|review your", re.I)
ALREADY_RE = re.compile(r"already (been )?(submitted|exists|listed|in (our|the) (directory|database))|"
                        r"duplicate|exists in (our|the) (directory|database)", re.I)
ERROR_RE = re.compile(r"(error|invalid|is required|please (enter|correct|fill|provide)|try again|could not|failed|"
                      r"not allowed|cannot be (blank|empty))", re.I)


def load_profiles():
    try:
        with open(PROFILES_PATH, "r", encoding="utf-8") as fh:
            return json.load(fh).get("profiles", {})
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        db.log("ERROR", "submitter", "data/submissions.json is not valid JSON", str(exc))
        return {}


def profile_for(domain):
    profiles = load_profiles()
    parts = domain.split(".")
    for i in range(len(parts) - 1):
        key = ".".join(parts[i:])
        if key in profiles:
            return profiles[key]
    return {}


# ------------------------------------------------------------- form parsing
def _label_for(form, el):
    text = []
    el_id = el.get("id")
    if el_id:
        lab = form.find("label", attrs={"for": el_id})
        if lab:
            text.append(lab.get_text(" ", strip=True))
    parent = el.find_parent("label")
    if parent:
        text.append(parent.get_text(" ", strip=True))
    for k in ("placeholder", "aria-label", "title"):
        if el.get(k):
            text.append(el[k])
    return " ".join(text)[:200]


def _is_hidden_trap(el):
    style = (el.get("style") or "").replace(" ", "").lower()
    if "display:none" in style or "visibility:hidden" in style or "left:-" in style:
        return True
    if el.get("hidden") is not None or el.get("aria-hidden") == "true" or el.get("tabindex") == "-1":
        return True
    ident = " ".join(str(el.get(k, "")) for k in ("name", "id", "class"))
    if isinstance(el.get("class"), list):
        ident = " ".join([str(el.get("name", "")), str(el.get("id", ""))] + el["class"])
    return bool(HONEYPOT_RE.search(ident))


def parse_forms(html, page_url):
    soup = crawler.soup_of(html)
    forms = []
    for form in soup.find_all("form"):
        fields, has_pw = [], False
        for el in form.find_all(["input", "textarea", "select"]):
            name = el.get("name")
            if not name:
                continue
            typ = (el.get("type") or ("textarea" if el.name == "textarea" else el.name)).lower()
            if typ == "password":
                has_pw = True
            if typ in ("submit", "button", "image", "reset", "file"):
                if typ == "file":
                    fields.append({"name": name, "type": "file", "required": el.get("required") is not None})
                elif typ in ("submit", "image") and name:
                    fields.append({"name": name, "type": "submit", "value": el.get("value", "")})
                continue
            field = {"name": name, "type": typ, "label": _label_for(form, el),
                     "required": el.get("required") is not None or el.get("aria-required") == "true",
                     "value": el.get("value", "") if el.name != "textarea" else el.get_text(),
                     "maxlength": int(el["maxlength"]) if str(el.get("maxlength", "")).isdigit() else None,
                     "trap": _is_hidden_trap(el), "checked": el.get("checked") is not None}
            if el.name == "select":
                field["options"] = [{"value": o.get("value", o.get_text(strip=True)), "label": o.get_text(strip=True)}
                                    for o in el.find_all("option")]
                chosen = el.find("option", selected=True)
                field["value"] = chosen.get("value", chosen.get_text(strip=True)) if chosen else ""
            fields.append(field)
        method = (form.get("method") or "GET").upper()
        action = urljoin(page_url, form.get("action") or page_url)
        forms.append({"action": action, "method": method, "fields": fields, "has_password": has_pw,
                      "captcha": bool(quality.CAPTCHA_RE.search(str(form))),
                      "text": form.get_text(" ", strip=True)[:500]})
    return forms


def pick_submission_form(forms):
    """Best form that looks like a site-submission form; never comment, login, search or newsletter forms."""
    best, best_score = None, 0
    for f in forms:
        names = {x["name"].lower() for x in f["fields"] if x.get("type") not in ("submit", "hidden")}
        if f["has_password"] or names & COMMENT_FIELDS or "wp-comments-post" in f["action"]:
            continue
        visible = [x for x in f["fields"] if x["type"] not in ("hidden", "submit") and not x.get("trap")]
        if len(visible) <= 1 and (not visible or visible[0]["name"].lower() in SEARCH_NAMES):
            continue
        score = 0
        for x in visible:
            blob = f"{x['name']} {x.get('label', '')}"
            if x["type"] == "url" or URL_RE.search(blob):
                score += 3
            elif DESC_RE.search(blob) or x["type"] == "textarea":
                score += 1
            elif NAME_RE.search(blob):
                score += 1
        if score >= 3 and score > best_score:
            best, best_score = f, score
    return best


def _match_option(options, wanted):
    want = quality.tokens(wanted)
    best, best_n = None, 0
    for o in options:
        if not o.get("value"):
            continue
        n = len(want & quality.tokens(o["label"]))
        if n > best_n:
            best, best_n = o["value"], n
    return best


def map_fields(form, target, profile=None):
    """Return (payload, issues, preview). `issues` block automation."""
    profile = profile or {}
    fmap = profile.get("field_map", {})
    payload, issues, preview = {}, [], []
    for f in form["fields"]:
        name, typ = f["name"], f["type"]
        if typ == "submit":
            payload.setdefault(name, f.get("value", ""))
            continue
        if typ == "file":
            if f["required"]:
                issues.append(f"Required file upload '{name}' cannot be filled automatically")
            continue
        if typ == "hidden":
            payload[name] = f.get("value", "")
            continue
        if f.get("trap"):
            payload[name] = ""          # honeypot: always left empty
            continue
        blob = f"{name} {f.get('label', '')}"
        value = None
        if name in fmap:
            spec = fmap[name]
            value = spec[8:] if spec.startswith("literal:") else {
                "url": target["url"], "name": target.get("name") or target["domain"],
                "description": target.get("description", ""), "email": target.get("contact_email", ""),
                "keywords": target.get("keywords", ""), "category": target.get("category", "")}.get(spec, "")
        elif typ in ("checkbox", "radio"):
            if f["required"] or CONSENT_RE.search(blob):
                if CONSENT_RE.search(blob):
                    if f["required"]:
                        issues.append(f"'{(f.get('label') or name)[:60]}' is a required consent box; "
                                      "only you can agree to a site's terms")
                    continue
            if f.get("checked"):
                payload[name] = f.get("value") or "on"
            continue
        elif typ == "select":
            if CATEGORY_RE.search(blob):
                value = _match_option(f.get("options", []), f"{target.get('category', '')} {target.get('keywords', '')}")
            elif COUNTRY_RE.search(blob):
                value = _match_option(f.get("options", []), target.get("countries", ""))
            elif LANG_RE.search(blob):
                value = _match_option(f.get("options", []), target.get("languages", ""))
            if value is None and f.get("value"):
                value = f["value"]
        elif typ == "email" or EMAIL_RE.search(blob):
            value = target.get("contact_email", "")
        elif typ == "url" or URL_RE.search(blob):
            value = target["url"]
        elif f["type"] == "textarea" or DESC_RE.search(blob):
            value = target.get("description", "")
        elif KEYW_RE.search(blob):
            value = target.get("keywords", "")
        elif NAME_RE.search(blob):
            value = target.get("name") or target["domain"]
        elif typ in ("text", "search", "tel", "number", "date"):
            value = f.get("value") or ""
        if value in (None, ""):
            value = f.get("value", "") if typ not in ("select",) else (value or "")
        if f["required"] and not value:
            issues.append(f"Required field '{(f.get('label') or name)[:60]}' has no matching information in your profile")
        if f.get("maxlength") and value and len(value) > f["maxlength"]:
            value = value[:f["maxlength"]]
        payload[name] = value
        if value:
            preview.append({"field": name, "value": value[:200]})
    return payload, issues, preview


# ------------------------------------------------------------- preparation
def _get(opp_id):
    opp = db.query("SELECT * FROM opportunities WHERE id=?", (opp_id,), one=True)
    if not opp:
        raise ValueError("Opportunity not found")
    target = db.query("SELECT * FROM targets WHERE id=?", (opp["target_id"],), one=True)
    return opp, target


def prepare(opp_id, fetcher, cfg=None):
    """Inspect the page and decide whether automatic submission is permitted. Sends nothing."""
    cfg = cfg or settings.load()
    opp, target = _get(opp_id)
    page = opp["submit_url"] or opp["url"]
    out = {"opportunity_id": opp_id, "can_auto": False, "reasons": [], "payload_preview": [],
           "manual_url": page, "form_action": "", "outcome_if_blocked": "manual_action",
           "target_summary": {"url": target["url"], "name": target["name"], "description": target["description"],
                              "email": target["contact_email"], "keywords": target["keywords"]}}
    prof = profile_for(opp["domain"])
    if prof.get("automation") == "manual_only":
        out["reasons"].append("Profile for this domain says manual only: " + prof.get("notes", ""))
        return out
    previous = db.query("SELECT status FROM submissions WHERE opportunity_id=? AND status IN "
                        "('submitted','awaiting_approval','pending_verification','already_submitted')", (opp_id,))
    if previous:
        out["reasons"].append("Already submitted earlier; not submitting again")
        out["outcome_if_blocked"] = "already_submitted"
        return out
    attempts = db.query("SELECT COUNT(*) c FROM submissions WHERE opportunity_id=? AND method='auto'",
                        (opp_id,), one=True)["c"]
    if attempts >= cfg["max_submission_attempts"]:
        out["reasons"].append(f"Attempt limit reached ({attempts}); submit manually")
        return out
    if opp["status"] not in ("eligible",):
        out["reasons"].append(f"Opportunity is classified '{opp['status']}', not 'eligible'")
        return out
    if cfg["require_approval"] and not opp["approved"]:
        out["reasons"].append("Waiting for your approval (Approve the opportunity first)")
        out["outcome_if_blocked"] = "awaiting_user_approval"
        return out
    res = fetcher.fetch(page)
    if res.error_kind == "robots":
        out["reasons"].append("robots.txt does not allow automated access to this page")
        return out
    if not res.ok:
        out["reasons"].append("Could not open the submission page: " + (res.error or f"HTTP {res.status}"))
        out["outcome_if_blocked"] = "failed"
        return out
    info = quality.analyze_page(res.text, res.final_url)
    if info["captcha"]:
        out["reasons"].append("CAPTCHA present; it will not be bypassed")
        out["outcome_if_blocked"] = "captcha_required"
        return out
    if info["has_password_field"] or info["login_text"] or info["register_text"]:
        out["reasons"].append("Login or registration is required")
        out["outcome_if_blocked"] = "login_required"
        return out
    if info["automation_hints"]:
        out["reasons"].append("The page mentions limits on automated submissions: " + "; ".join(info["automation_hints"]))
        return out
    form = pick_submission_form(parse_forms(res.text, res.final_url))
    if not form:
        out["reasons"].append("No usable submission form found on the page")
        return out
    out["form_action"] = form["action"]
    if form["captcha"]:
        out["reasons"].append("The form contains a CAPTCHA")
        out["outcome_if_blocked"] = "captcha_required"
        return out
    try:
        action = safety.validate_url(form["action"])
    except safety.UnsafeURL as exc:
        out["reasons"].append(f"Form destination is not safe: {exc}")
        return out
    if not safety.same_site(action, res.final_url):
        out["reasons"].append("The form posts to a different website; not sending your details there automatically")
        return out
    allowed, why, _ = fetcher.robots(action)
    if not allowed:
        out["reasons"].append(f"robots.txt forbids automated requests to the form action ({why})")
        return out
    payload, issues, preview = map_fields(form, target, prof)
    out["payload_preview"] = preview
    if issues:
        out["reasons"].extend(issues)
        return out
    out.update(can_auto=True, _form=form, _payload=payload, _action=action, _page=res.final_url)
    out["reasons"].append("All automatic-submission checks passed")
    return out


# ------------------------------------------------------------- outcome
def classify_response(res, form_action=""):
    """Map an HTTP response to a submission outcome. Never returns a 'live' claim."""
    if res.error_kind == "robots":
        return "manual_action", "Blocked by robots.txt"
    if res.error:
        return "failed", res.error
    s = res.status or 0
    text = res.text or ""
    lower = text[:200000]
    if quality.CAPTCHA_RE.search(lower):
        return "captcha_required", "The site is asking for a CAPTCHA"
    if s == 401:
        return "login_required", "HTTP 401: authentication required"
    if s == 403:
        return "manual_action", "HTTP 403: the site refused the request (anti-bot or permissions)"
    if s == 429:
        return "failed", "HTTP 429: rate limited. Do not retry immediately"
    if s >= 400:
        return "failed", f"HTTP {s}"
    plain = crawler.soup_of(text).get_text(" ", strip=True)
    if ALREADY_RE.search(plain):
        return "already_submitted", "Site reports the URL already exists"
    again = pick_submission_form(parse_forms(text, res.final_url)) if text else None
    if SUCCESS_RE.search(plain):
        if AWAIT_RE.search(plain):
            return "awaiting_approval", "Site says the submission is waiting for review"
        return "submitted", "Site acknowledged the submission (not proof that a link was published)"
    if AWAIT_RE.search(plain) and not again:
        return "awaiting_approval", "Response mentions review/approval"
    if again or ERROR_RE.search(plain[:3000]):
        m = ERROR_RE.search(plain[:3000])
        snippet = plain[max(0, m.start() - 60):m.end() + 100] if m else "form displayed again"
        return "failed", "Site did not accept the form: " + safety.clean_text(snippet, 200)
    return "pending_verification", f"HTTP {s} but no clear confirmation; verify later"


def _record(opp, target, status, method, destination, http_status, detail, payload, cfg, listing=""):
    ts = db.now()
    verify_after = db.hours_from_now(cfg["verify_delay_hours"]) if status in DONE_STATUSES else None
    res = db.execute("""INSERT INTO submissions (opportunity_id, target_id, status, method, destination_url, http_status,
                         detail, payload, listing_url, submitted_at, verify_after) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                     (opp["id"], target["id"], status, method, destination, http_status, detail[:600],
                      db.dumps(payload), listing, ts, verify_after))
    if status in DONE_STATUSES:
        source = listing or opp["url"]
        db.execute("""INSERT OR IGNORE INTO backlinks (target_id, opportunity_id, submission_id, source_url, state,
                       next_check, created_at) VALUES (?,?,?,?, 'pending', ?, ?)""",
                   (target["id"], opp["id"], res.lastrowid, source, verify_after, ts))
    db.log("INFO" if status in DONE_STATUSES else "WARNING", "submitter",
           f"{opp['domain']}: {status}", detail[:300], target["id"])
    return res.lastrowid


def submit(opp_id, fetcher, cfg=None):
    """Attempt one automatic submission. Always records an outcome. Returns a result dict."""
    cfg = cfg or settings.load()
    opp, target = _get(opp_id)
    prep = prepare(opp_id, fetcher, cfg)
    if not prep["can_auto"]:
        status = prep["outcome_if_blocked"]
        if status == "awaiting_user_approval":
            return {"status": "skipped", "detail": "; ".join(prep["reasons"]), "recorded": False}
        if status == "already_submitted" and not db.query(
                "SELECT 1 FROM submissions WHERE opportunity_id=? AND status='already_submitted'", (opp_id,)):
            return {"status": "skipped", "detail": "; ".join(prep["reasons"]), "recorded": False}
        detail = "; ".join(prep["reasons"])
        _record(opp, target, status, "auto", prep["manual_url"], None, detail,
                {"preview": prep["payload_preview"]}, cfg)
        return {"status": status, "detail": detail, "manual_url": prep["manual_url"], "recorded": True}
    headers = {"Referer": prep["_page"]}
    if prep["_form"]["method"] == "POST":
        res = fetcher.fetch(prep["_action"], method="POST", data=prep["_payload"], extra_headers=headers)
    else:   # GET forms carry their fields in the query string
        from urllib.parse import urlencode
        res = fetcher.fetch(prep["_action"] + ("&" if "?" in prep["_action"] else "?") + urlencode(prep["_payload"]),
                            extra_headers=headers)
    status, detail = classify_response(res, prep["_action"])
    stored = {k: v for k, v in prep["_payload"].items() if v != "" and not k.lower().startswith(("csrf", "token", "_"))}
    _record(opp, target, status, "auto", prep["_action"], res.status, detail, stored, cfg)
    return {"status": status, "detail": detail, "http_status": res.status, "recorded": True}


def record_manual(opp_id, listing_url="", note=""):
    """User says they submitted by hand: store it and schedule verification."""
    cfg = settings.load()
    opp, target = _get(opp_id)
    listing = ""
    if listing_url.strip():
        listing = safety.normalize_url(safety.validate_url(safety.normalize_user_url(listing_url), resolve=False))
    sid = _record(opp, target, "pending_verification", "manual", opp["submit_url"] or opp["url"], None,
                  safety.clean_text(note, 300) or "Submitted manually by the user", {}, cfg, listing)
    return sid
