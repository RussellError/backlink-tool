"""Numbers shown on the dashboard. Every figure comes straight from the database."""
from . import db

LIVE_SQL = "('live','live_nofollow','live_sponsored','live_ugc')"


def stats(target_id):
    q = lambda sql, p=(): db.query(sql, p, one=True)
    target = q("SELECT * FROM targets WHERE id=?", (target_id,))
    if not target:
        return None
    by_status = {r["status"]: r["c"] for r in db.query(
        "SELECT status, COUNT(*) c FROM opportunities WHERE target_id=? GROUP BY status", (target_id,))}
    by_category = {r["category"]: r["c"] for r in db.query(
        "SELECT category, COUNT(*) c FROM opportunities WHERE target_id=? GROUP BY category", (target_id,))}
    sub = lambda where: q(f"SELECT COUNT(*) c FROM submissions WHERE target_id=? AND {where}", (target_id,))["c"]
    sub_status = {r["status"]: r["c"] for r in db.query(
        "SELECT status, COUNT(*) c FROM submissions WHERE target_id=? GROUP BY status", (target_id,))}
    recheck = q(f"""SELECT COUNT(*) c FROM backlinks WHERE target_id=? AND (state IN
                    ('pending','temporary_failure','inconclusive') OR (next_check IS NOT NULL AND next_check<=?))""",
                (target_id, db.now()))["c"]
    return {
        "target": target,
        "total_opportunities": sum(by_status.values()),
        "awaiting_review": by_status.get("review", 0) + by_status.get("new", 0),
        "eligible": by_status.get("eligible", 0),
        "submission_attempts": sub("method='auto'"),
        "manual_recorded": sub("method='manual'"),
        "confirmed_live": q(f"SELECT COUNT(*) c FROM backlinks WHERE target_id=? AND state IN {LIVE_SQL}",
                            (target_id,))["c"],
        "failed_submissions": sub("status IN ('failed','rejected')"),
        "needs_recheck": recheck,
        "by_status": by_status, "by_category": by_category, "submission_status": sub_status,
        "backlink_states": {r["state"]: r["c"] for r in db.query(
            "SELECT state, COUNT(*) c FROM backlinks WHERE target_id=? GROUP BY state", (target_id,))},
    }


def activity(target_id=None, limit=30, level=None):
    sql, params = "SELECT * FROM logs WHERE 1=1", []
    if target_id:
        sql += " AND (target_id=? OR target_id IS NULL)"
        params.append(target_id)
    if level:
        sql += " AND level=?"
        params.append(level.upper())
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit), 500)))
    return db.query(sql, params)
