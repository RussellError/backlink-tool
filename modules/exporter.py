"""CSV / JSON exports. A copy of each export is also written to reports/."""
import csv
import io
import os

from . import db

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR = os.path.join(BASE_DIR, "reports")
COLUMNS = ["source_website", "opportunity_url", "target_url", "opportunity_category", "quality_assessment",
           "submission_status", "http_status", "anchor_text", "link_attributes", "first_discovered",
           "last_verified", "error_details"]
KINDS = ("opportunities", "submissions", "backlinks")
DISCLAIMER = ("A discovered opportunity is not a confirmed backlink. Quality scores are heuristics built from public "
              "page content, not Domain Authority or traffic. Only rows in state live* are confirmed backlinks.")


def _quality(row):
    if row.get("score") is None:
        return "not assessed"
    return f"{row['score']}/100 (heuristic); class={row.get('status', '')}; {row.get('summary', '')}".strip()


def rows_for(kind, target_id):
    if kind == "opportunities":
        data = db.query("""SELECT o.*, a.score, a.summary, a.http_status AS a_http, a.assessed_at, t.url AS target_url,
                           (SELECT s.status FROM submissions s WHERE s.opportunity_id=o.id ORDER BY s.id DESC LIMIT 1) AS sub_status
                           FROM opportunities o JOIN targets t ON t.id=o.target_id
                           LEFT JOIN assessments a ON a.opportunity_id=o.id WHERE o.target_id=? ORDER BY o.id""",
                        (target_id,))
        return [{"source_website": r["domain"], "opportunity_url": r["url"], "target_url": r["target_url"],
                 "opportunity_category": r["category"], "quality_assessment": _quality(r),
                 "submission_status": r["sub_status"] or "", "http_status": r["a_http"] or "", "anchor_text": "",
                 "link_attributes": "", "first_discovered": r["discovered_at"], "last_verified": r["assessed_at"] or "",
                 "error_details": r["last_error"]} for r in data]
    if kind == "submissions":
        data = db.query("""SELECT s.*, o.domain, o.url AS opp_url, o.category, t.url AS target_url, a.score, a.summary,
                           o.status AS opp_status, o.discovered_at, o.last_error,
                           (SELECT b.state FROM backlinks b WHERE b.submission_id=s.id) AS bl_state
                           FROM submissions s JOIN opportunities o ON o.id=s.opportunity_id
                           JOIN targets t ON t.id=s.target_id LEFT JOIN assessments a ON a.opportunity_id=o.id
                           WHERE s.target_id=? ORDER BY s.id""", (target_id,))
        return [{"source_website": r["domain"], "opportunity_url": r["opp_url"], "target_url": r["target_url"],
                 "opportunity_category": r["category"],
                 "quality_assessment": _quality({"score": r["score"], "status": r["opp_status"], "summary": r["summary"]}),
                 "submission_status": f"{r['status']} ({r['method']})", "http_status": r["http_status"] or "",
                 "anchor_text": "", "link_attributes": "", "first_discovered": r["discovered_at"],
                 "last_verified": r["submitted_at"], "error_details": r["detail"] if r["status"] in
                 ("failed", "rejected", "manual_action", "captcha_required", "login_required") else ""} for r in data]
    if kind == "backlinks":
        data = db.query("""SELECT b.*, t.url AS target_url, o.domain AS opp_domain, o.category, o.status AS opp_status,
                           a.score, a.summary,
                           (SELECT s.status FROM submissions s WHERE s.id=b.submission_id) AS sub_status
                           FROM backlinks b JOIN targets t ON t.id=b.target_id
                           LEFT JOIN opportunities o ON o.id=b.opportunity_id
                           LEFT JOIN assessments a ON a.opportunity_id=o.id WHERE b.target_id=? ORDER BY b.id""",
                        (target_id,))
        from . import safety
        return [{"source_website": r["opp_domain"] or safety.get_domain(r["source_url"]), "opportunity_url": r["source_url"],
                 "target_url": r["target_url"], "opportunity_category": r["category"] or "manual",
                 "quality_assessment": _quality({"score": r["score"], "status": r["opp_status"], "summary": r["summary"]}),
                 "submission_status": f"{r['sub_status'] or ''}", "http_status": r["http_status"] or "",
                 "anchor_text": r["anchor_text"], "link_attributes": r["rel"] or ("dofollow" if r["state"] == "live" else ""),
                 "first_discovered": r["first_seen"] or r["created_at"], "last_verified": r["last_checked"] or "",
                 "error_details": f"state={r['state']}; {r['error']}".strip("; ")} for r in data]
    raise ValueError("Unknown export type")


def _csv_safe(value):
    """Neutralise spreadsheet formula injection (cells starting with = + - @)."""
    text = "" if value is None else str(value)
    return "'" + text if text[:1] in ("=", "+", "-", "@", "\t", "\r") else text


def export(kind, fmt, target_id):
    if kind not in KINDS or fmt not in ("csv", "json"):
        raise ValueError("Unknown export type or format")
    rows = rows_for(kind, target_id)
    stamp = db.now().replace(":", "").replace("-", "")
    name = f"{kind}_target{target_id}_{stamp}.{fmt}"
    if fmt == "csv":
        buf = io.StringIO()
        w = csv.DictWriter(buf, fieldnames=COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: _csv_safe(r.get(k, "")) for k in COLUMNS})
        data, mime = buf.getvalue().encode("utf-8-sig"), "text/csv; charset=utf-8"
    else:
        data = db.dumps({"generated_at": db.now(), "kind": kind, "disclaimer": DISCLAIMER,
                         "count": len(rows), "rows": rows}).encode("utf-8")
        mime = "application/json"
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(os.path.join(REPORTS_DIR, name), "wb") as fh:
        fh.write(data)
    return name, mime, data
