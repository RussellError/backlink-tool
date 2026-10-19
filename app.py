"""Backlink Builder - local Flask app. Binds to 127.0.0.1 only."""
import logging
import os
import sqlite3
from logging.handlers import RotatingFileHandler

from flask import Flask, Response, jsonify, render_template, request

from modules import (checker, crawler, db, exporter, finder, jobs, outreach, quality, reporter, safety,
                     settings, submitter)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
HOST = "127.0.0.1"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "BacklinkBuilder"


def setup_logging():
    os.makedirs(os.path.join(BASE_DIR, "logs"), exist_ok=True)
    handler = RotatingFileHandler(os.path.join(BASE_DIR, "logs", "app.log"), maxBytes=512 * 1024, backupCount=3,
                                  encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    db.logger.setLevel(logging.INFO)
    if not any(isinstance(h, RotatingFileHandler) for h in db.logger.handlers):
        db.logger.addHandler(handler)


class ApiError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message, self.status = message, status


def create_app(start_background=True):
    setup_logging()
    db.init_db()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = 3 * 1024 * 1024
    app.config["JSON_SORT_KEYS"] = False
    port = int(os.environ.get("BACKLINK_PORT") or settings.load()["port"])
    app.config["ALLOWED_HOSTS"] = {f"127.0.0.1:{port}", f"localhost:{port}"}
    if start_background:
        jobs.recover_on_startup()

    # ------------------------------------------------------------ security
    @app.before_request
    def guard():
        host = request.headers.get("Host", "")
        if host not in app.config["ALLOWED_HOSTS"]:        # DNS-rebinding protection
            return Response("Forbidden host", status=403)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            if request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                return jsonify(error="Missing request header (possible cross-site request)"), 403
            origin = request.headers.get("Origin")
            if origin and origin.split("://", 1)[-1] not in app.config["ALLOWED_HOSTS"]:
                return jsonify(error="Cross-origin request blocked"), 403

    @app.after_request
    def headers(resp):
        resp.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self'; style-src 'self'; "
                                                   "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; "
                                                   "form-action 'self'")
        resp.headers["X-Content-Type-Options"] = "nosniff"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Referrer-Policy"] = "no-referrer"
        if request.path.startswith("/api/"):
            resp.headers["Cache-Control"] = "no-store"
        return resp

    @app.errorhandler(ApiError)
    def api_error(e):
        return jsonify(error=e.message), e.status

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify(error="Not found"), 404
        return render_template("index.html", page="index"), 404

    @app.errorhandler(413)
    def too_big(e):
        return jsonify(error="Upload is larger than 3 MB"), 413

    @app.errorhandler(Exception)
    def crash(e):
        if hasattr(e, "code") and isinstance(e.code, int) and e.code < 500:
            return jsonify(error=getattr(e, "description", "Request error")), e.code
        db.log("ERROR", "web", f"Unhandled error on {request.path}", repr(e))
        return jsonify(error="Internal error. Details were written to the activity log."), 500

    # ------------------------------------------------------------ helpers
    def body():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            raise ApiError("Expected a JSON object")
        return data

    def target_id_arg(required=True):
        raw = request.args.get("target_id") or (request.get_json(silent=True) or {}).get("target_id")
        try:
            tid = int(raw)
        except (TypeError, ValueError):
            if required:
                raise ApiError("target_id is required")
            return None
        if not db.query("SELECT 1 FROM targets WHERE id=?", (tid,)):
            raise ApiError("Website not found", 404)
        return tid

    def opp_or_404(opp_id):
        opp = db.query("SELECT * FROM opportunities WHERE id=?", (opp_id,), one=True)
        if not opp:
            raise ApiError("Opportunity not found", 404)
        return opp

    def paging():
        try:
            limit = max(1, min(int(request.args.get("limit", 50)), 200))
            offset = max(0, int(request.args.get("offset", 0)))
        except ValueError:
            raise ApiError("limit and offset must be numbers")
        return limit, offset

    # ------------------------------------------------------------ pages
    for route, tpl in (("/", "index"), ("/opportunities", "opportunities"), ("/backlinks", "backlinks"),
                       ("/settings", "settings"), ("/history", "history")):
        app.add_url_rule(route, tpl, (lambda t: lambda: render_template(f"{t}.html", page=t))(tpl))

    @app.get("/api/health")
    def health():
        db.query("SELECT 1")
        return jsonify(ok=True, host=HOST, db=os.path.basename(db.DB_PATH))

    # ------------------------------------------------------------ targets
    def target_fields(data):
        try:
            url = safety.normalize_url(safety.validate_url(safety.normalize_user_url(data.get("url", "")),
                                                           resolve=False))
        except safety.UnsafeURL as exc:
            raise ApiError(f"Website URL: {exc}")
        email = safety.clean_text(data.get("contact_email", ""), 254)
        if email and not safety.valid_email(email):
            raise ApiError("Contact email does not look valid")
        types = data.get("opp_types", [])
        if isinstance(types, str):
            types = [t for t in types.split(",") if t]
        if any(t not in finder.CATEGORIES for t in types):
            raise ApiError("Unknown opportunity type")
        listy = lambda key, n: ", ".join([safety.clean_text(x, 60) for x in
                                          str(data.get(key, "")).replace("\n", ",").split(",") if x.strip()][:n])
        return {"url": url, "domain": safety.get_domain(url), "name": safety.clean_text(data.get("name"), 120),
                "description": safety.clean_text(data.get("description"), 600),
                "category": safety.clean_text(data.get("category"), 80), "keywords": listy("keywords", 20),
                "languages": listy("languages", 10), "countries": listy("countries", 10),
                "contact_email": email, "opp_types": ",".join(types)}

    @app.get("/api/targets")
    def targets_list():
        return jsonify(targets=db.query("SELECT * FROM targets ORDER BY updated_at DESC"),
                       categories=finder.CATEGORIES)

    @app.post("/api/targets")
    def targets_save():
        data = body()
        f = target_fields(data)
        ts = db.now()
        tid = data.get("id")
        if tid:
            if not db.query("SELECT 1 FROM targets WHERE id=?", (tid,)):
                raise ApiError("Website not found", 404)
            try:
                db.execute("""UPDATE targets SET url=?, domain=?, name=?, description=?, category=?, keywords=?,
                              languages=?, countries=?, contact_email=?, opp_types=?, updated_at=? WHERE id=?""",
                           (f["url"], f["domain"], f["name"], f["description"], f["category"], f["keywords"],
                            f["languages"], f["countries"], f["contact_email"], f["opp_types"], ts, tid))
            except sqlite3.IntegrityError:
                raise ApiError("Another saved website already uses that URL")
        else:
            try:
                tid = db.execute("""INSERT INTO targets (url, domain, name, description, category, keywords, languages,
                                    countries, contact_email, opp_types, created_at, updated_at)
                                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                                 (f["url"], f["domain"], f["name"], f["description"], f["category"], f["keywords"],
                                  f["languages"], f["countries"], f["contact_email"], f["opp_types"], ts, ts)).lastrowid
            except sqlite3.IntegrityError:
                existing = db.query("SELECT id FROM targets WHERE url=?", (f["url"],), one=True)
                raise ApiError("That website is already saved", 409) if existing else ApiError("Could not save")
        return jsonify(target=db.query("SELECT * FROM targets WHERE id=?", (tid,), one=True))

    # ------------------------------------------------------------ dashboard
    @app.get("/api/state")
    def state():
        tid = target_id_arg()
        return jsonify(stats=reporter.stats(tid),
                       jobs=db.query("SELECT * FROM jobs WHERE target_id=? AND status IN "
                                     "('queued','running','paused','interrupted') ORDER BY id DESC", (tid,)),
                       recent_jobs=db.query("SELECT * FROM jobs WHERE target_id=? ORDER BY id DESC LIMIT 5", (tid,)),
                       activity=reporter.activity(tid, 25), providers=settings.provider_status())

    # ------------------------------------------------------------ jobs
    @app.post("/api/jobs")
    def jobs_start():
        data = body()
        tid = target_id_arg()
        kind = data.get("kind")
        if kind not in jobs.HANDLERS:
            raise ApiError("Unknown job type")
        params = {}
        if kind == "discovery":
            refs = data.get("reference_urls") or []
            if isinstance(refs, str):
                refs = [r.strip() for r in refs.splitlines() if r.strip()]
            clean = []
            for r in refs[:10]:
                try:
                    clean.append(safety.validate_url(safety.normalize_user_url(r), resolve=False))
                except safety.UnsafeURL as exc:
                    raise ApiError(f"Reference URL '{r[:60]}': {exc}")
            params["reference_urls"] = clean
        if kind in ("submit", "evaluate") and data.get("ids"):
            try:
                params["ids"] = [int(i) for i in data["ids"]][:500]
            except (TypeError, ValueError):
                raise ApiError("ids must be numbers")
        if kind == "verify":
            params["force"] = bool(data.get("force"))
        try:
            job = jobs.start(kind, tid, params)
        except jobs.JobConflict as exc:
            raise ApiError(str(exc), 409)
        return jsonify(job=job)

    @app.get("/api/jobs")
    def jobs_list():
        tid = target_id_arg()
        limit, offset = paging()
        return jsonify(jobs=db.query("SELECT * FROM jobs WHERE target_id=? ORDER BY id DESC LIMIT ? OFFSET ?",
                                     (tid, limit, offset)))

    @app.post("/api/jobs/<int:job_id>/<action>")
    def jobs_action(job_id, action):
        fn = {"pause": jobs.pause, "resume": jobs.resume, "cancel": jobs.cancel}.get(action)
        if not fn:
            raise ApiError("Unknown action", 404)
        try:
            fn(job_id)
        except ValueError as exc:
            raise ApiError(str(exc), 409)
        return jsonify(job=db.query("SELECT * FROM jobs WHERE id=?", (job_id,), one=True))

    # ------------------------------------------------------------ opportunities
    @app.get("/api/opportunities")
    def opps_list():
        tid = target_id_arg()
        limit, offset = paging()
        where, params = ["o.target_id=?"], [tid]
        status = request.args.get("status")
        if status:
            if status not in db.OPP_STATUSES:
                raise ApiError("Unknown status")
            where.append("o.status=?"); params.append(status)
        cat = request.args.get("category")
        if cat:
            where.append("o.category=?"); params.append(cat)
        q = safety.clean_text(request.args.get("q", ""), 80)
        if q:
            where.append("(o.url LIKE ? OR o.title LIKE ? OR o.domain LIKE ?)")
            params += [f"%{q}%"] * 3
        base = " FROM opportunities o LEFT JOIN assessments a ON a.opportunity_id=o.id WHERE " + " AND ".join(where)
        total = db.query("SELECT COUNT(*) c" + base, params, one=True)["c"]
        rows = db.query("""SELECT o.*, a.score, a.summary, a.http_status,
                           (SELECT s.status FROM submissions s WHERE s.opportunity_id=o.id ORDER BY s.id DESC LIMIT 1)
                           AS submission_status""" + base + " ORDER BY o.id DESC LIMIT ? OFFSET ?",
                        params + [limit, offset])
        return jsonify(opportunities=rows, total=total, categories=finder.CATEGORIES)

    @app.get("/api/opportunities/<int:opp_id>")
    def opp_detail(opp_id):
        opp = opp_or_404(opp_id)
        a = db.query("SELECT * FROM assessments WHERE opportunity_id=?", (opp_id,), one=True)
        if a:
            for k in ("verified", "estimated", "breakdown", "flags"):
                a[k] = db.loads(a[k], {} if k in ("verified", "estimated") else [])
        return jsonify(opportunity=opp, assessment=a,
                       submissions=db.query("SELECT * FROM submissions WHERE opportunity_id=? ORDER BY id DESC", (opp_id,)),
                       drafts=db.query("SELECT * FROM outreach_drafts WHERE opportunity_id=? ORDER BY id DESC", (opp_id,)),
                       backlinks=db.query("SELECT * FROM backlinks WHERE opportunity_id=?", (opp_id,)))

    @app.post("/api/opportunities")
    def opp_add():
        data = body()
        tid = target_id_arg()
        try:
            oid, created = finder.add_opportunity(tid, data.get("url", ""), data.get("category", ""), source="manual",
                                                  title=data.get("title", ""), notes=data.get("notes", ""))
        except (safety.UnsafeURL, ValueError) as exc:
            raise ApiError(str(exc))
        return jsonify(id=oid, created=created,
                       message="Added. Run 'Assess' to evaluate it." if created else "Already saved (duplicate skipped).")

    @app.post("/api/opportunities/import")
    def opp_import():
        tid = target_id_arg()
        f = request.files.get("file")
        if not f or not f.filename:
            raise ApiError("Choose a .txt or .csv file")
        if not f.filename.lower().endswith((".txt", ".csv")):
            raise ApiError("Only .txt and .csv files are supported")
        try:
            stats = finder.import_urls(tid, f.filename, f.read(), request.form.get("category", "resource_page"))
        except ValueError as exc:
            raise ApiError(str(exc))
        return jsonify(stats)

    @app.post("/api/opportunities/<int:opp_id>/status")
    def opp_status(opp_id):
        opp_or_404(opp_id)
        status = body().get("status")
        if status not in db.OPP_STATUSES or status == "new":
            raise ApiError("Choose a valid status")
        db.execute("UPDATE opportunities SET status=?, user_override=1, approved=CASE WHEN ?='eligible' THEN approved ELSE 0 END, "
                   "updated_at=? WHERE id=?", (status, status, db.now(), opp_id))
        return jsonify(ok=True)

    @app.post("/api/opportunities/<int:opp_id>/approve")
    def opp_approve(opp_id):
        opp = opp_or_404(opp_id)
        approve = bool(body().get("approved", True))
        if approve and opp["status"] != "eligible":
            raise ApiError("Only opportunities classified 'eligible' can be approved for automatic submission", 409)
        db.execute("UPDATE opportunities SET approved=?, updated_at=? WHERE id=?", (int(approve), db.now(), opp_id))
        return jsonify(ok=True)

    @app.post("/api/opportunities/approve-all")
    def opp_approve_all():
        tid = target_id_arg()
        r = db.execute("UPDATE opportunities SET approved=1, updated_at=? WHERE target_id=? AND status='eligible' "
                       "AND approved=0", (db.now(), tid))
        return jsonify(approved=r.rowcount)

    @app.post("/api/opportunities/<int:opp_id>/prepare")
    def opp_prepare(opp_id):
        opp_or_404(opp_id)
        prep = submitter.prepare(opp_id, crawler.Fetcher())
        return jsonify({k: v for k, v in prep.items() if not k.startswith("_")})

    @app.post("/api/opportunities/<int:opp_id>/manual-submission")
    def opp_manual(opp_id):
        opp_or_404(opp_id)
        data = body()
        try:
            sid = submitter.record_manual(opp_id, data.get("listing_url", ""), data.get("note", ""))
        except (safety.UnsafeURL, ValueError) as exc:
            raise ApiError(str(exc))
        return jsonify(submission_id=sid, message="Recorded. A verification check is scheduled; this is not a "
                                                  "confirmed backlink until the link is found.")

    @app.post("/api/opportunities/<int:opp_id>/outreach")
    def opp_outreach(opp_id):
        opp_or_404(opp_id)
        return jsonify(draft=outreach.create_draft(opp_id))

    @app.post("/api/outreach/bulk")
    def outreach_bulk():
        tid = target_id_arg()
        ids = [r["id"] for r in db.query("SELECT id FROM opportunities WHERE target_id=? AND status='outreach'", (tid,))]
        for i in ids:
            outreach.create_draft(i)
        return jsonify(count=len(ids))

    # ------------------------------------------------------------ submissions / outreach / logs
    @app.get("/api/submissions")
    def submissions_list():
        tid = target_id_arg()
        limit, offset = paging()
        return jsonify(submissions=db.query(
            """SELECT s.*, o.url AS opportunity_url, o.domain FROM submissions s JOIN opportunities o
               ON o.id=s.opportunity_id WHERE s.target_id=? ORDER BY s.id DESC LIMIT ? OFFSET ?""",
            (tid, limit, offset)))

    @app.get("/api/outreach")
    def outreach_list():
        tid = target_id_arg()
        return jsonify(drafts=db.query(
            """SELECT d.*, o.domain, o.url FROM outreach_drafts d JOIN opportunities o ON o.id=d.opportunity_id
               WHERE d.target_id=? ORDER BY d.id DESC LIMIT 200""", (tid,)))

    @app.get("/api/logs")
    def logs_list():
        tid = target_id_arg(required=False)
        return jsonify(logs=reporter.activity(tid, request.args.get("limit", 100), request.args.get("level")))

    @app.get("/api/verifications")
    def verifications():
        tid = target_id_arg()
        limit, offset = paging()
        return jsonify(verifications=db.query(
            """SELECT v.*, b.source_url FROM verification_history v JOIN backlinks b ON b.id=v.backlink_id
               WHERE b.target_id=? ORDER BY v.id DESC LIMIT ? OFFSET ?""", (tid, limit, offset)))

    # ------------------------------------------------------------ backlinks
    @app.get("/api/backlinks")
    def backlinks_list():
        tid = target_id_arg()
        limit, offset = paging()
        where, params = "b.target_id=?", [tid]
        state = request.args.get("state")
        if state:
            if state not in db.BACKLINK_STATES:
                raise ApiError("Unknown state")
            where += " AND b.state=?"; params.append(state)
        total = db.query(f"SELECT COUNT(*) c FROM backlinks b WHERE {where}", params, one=True)["c"]
        rows = db.query(f"SELECT b.* FROM backlinks b WHERE {where} ORDER BY b.id DESC LIMIT ? OFFSET ?",
                        params + [limit, offset])
        return jsonify(backlinks=rows, total=total)

    @app.post("/api/backlinks")
    def backlinks_add():
        data = body()
        tid = target_id_arg()
        try:
            bid, created = checker.add_backlink(tid, data.get("source_url", ""))
        except (safety.UnsafeURL, ValueError) as exc:
            raise ApiError(str(exc))
        return jsonify(id=bid, created=created)

    @app.post("/api/backlinks/<int:bl_id>/check")
    def backlinks_check(bl_id):
        if not db.query("SELECT 1 FROM backlinks WHERE id=?", (bl_id,)):
            raise ApiError("Backlink not found", 404)
        result = checker.run_check(bl_id, crawler.Fetcher())
        return jsonify(result=result, backlink=db.query("SELECT * FROM backlinks WHERE id=?", (bl_id,), one=True))

    @app.get("/api/backlinks/<int:bl_id>/history")
    def backlinks_history(bl_id):
        return jsonify(history=db.query("SELECT * FROM verification_history WHERE backlink_id=? ORDER BY id DESC LIMIT 50",
                                        (bl_id,)))

    # ------------------------------------------------------------ settings / export
    @app.get("/api/settings")
    def settings_get():
        return jsonify(settings=settings.load(), providers=settings.provider_status(), defaults=settings.DEFAULTS)

    @app.post("/api/settings")
    def settings_save():
        try:
            cfg = settings.update(body())
        except settings.SettingsError as exc:
            raise ApiError(str(exc))
        return jsonify(settings=cfg, note="Saved. Port changes apply after restarting the app.")

    @app.get("/export/<kind>.<fmt>")
    def export_file(kind, fmt):
        tid = target_id_arg()
        try:
            name, mime, data = exporter.export(kind, fmt, tid)
        except ValueError as exc:
            raise ApiError(str(exc), 404)
        return Response(data, mimetype=mime.split(";")[0],
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    return app


if __name__ == "__main__":
    application = create_app()
    port = int(os.environ.get("BACKLINK_PORT") or settings.load()["port"])
    print(f"\n  Backlink Builder is running. Open http://127.0.0.1:{port} in your browser.")
    print("  Stop with Ctrl+C.  Bound to localhost only; not reachable from other devices.\n")
    application.run(host=HOST, port=port, threaded=True, debug=False, use_reloader=False)
