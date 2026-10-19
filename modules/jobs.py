"""Background jobs: discovery, evaluate, submit, verify.

* one active job per (target, kind) - enforced by a partial unique index in SQLite
* pause / resume / cancel, checked between units of work
* progress + checkpoint saved after every unit, so a restart can continue
* after a restart, discovery / evaluate / verify jobs auto-resume (if enabled);
  submit jobs are NEVER auto-resumed because they can publish content
"""
import sqlite3
import threading
import time
import traceback

from . import checker, crawler, db, finder, quality, settings, submitter

SAFE_TO_AUTO_RESUME = ("discovery", "evaluate", "verify")
_controls = {}               # job_id -> {"pause": Event, "cancel": Event, "thread": Thread}
_lock = threading.Lock()


class JobConflict(Exception):
    pass


class JobContext:
    def __init__(self, row):
        self.id, self.target_id, self.kind = row["id"], row["target_id"], row["kind"]
        self.params = db.loads(row["params"], {})
        self.checkpoint = db.loads(row["checkpoint"], {})
        self._c = _controls[self.id]

    @property
    def cancelled(self):
        return self._c["cancel"].is_set()

    def should_stop(self):
        return self.cancelled

    def wait_if_paused(self):
        while self._c["pause"].is_set() and not self.cancelled:
            time.sleep(0.3)

    def update(self, progress=None, total=None, message=None):
        sets, params = ["updated_at=?"], [db.now()]
        for col, val in (("progress", progress), ("total", total), ("message", message)):
            if val is not None:
                sets.append(f"{col}=?")
                params.append(val if col != "message" else str(val)[:300])
        db.execute(f"UPDATE jobs SET {', '.join(sets)} WHERE id=?", params + [self.id])

    def save_checkpoint(self, data):
        self.checkpoint.update(data)
        db.execute("UPDATE jobs SET checkpoint=?, updated_at=? WHERE id=?",
                   (db.dumps(self.checkpoint), db.now(), self.id))


def start(kind, target_id, params=None):
    if not db.query("SELECT 1 FROM targets WHERE id=?", (target_id,)):
        raise ValueError("Unknown target website")
    ts = db.now()
    try:
        res = db.execute("INSERT INTO jobs (target_id, kind, status, params, created_at, updated_at) "
                         "VALUES (?,?, 'queued', ?, ?, ?)", (target_id, kind, db.dumps(params or {}), ts, ts))
    except sqlite3.IntegrityError:
        raise JobConflict(f"A {kind} job for this website is already active. Pause, resume or cancel it first.")
    _spawn(res.lastrowid)
    return db.query("SELECT * FROM jobs WHERE id=?", (res.lastrowid,), one=True)


def _spawn(job_id):
    with _lock:
        _controls[job_id] = {"pause": threading.Event(), "cancel": threading.Event(), "thread": None}
        t = threading.Thread(target=_run, args=(job_id,), daemon=True, name=f"job-{job_id}")
        _controls[job_id]["thread"] = t
    t.start()


def _alive(job_id):
    c = _controls.get(job_id)
    return bool(c and c["thread"] and c["thread"].is_alive())


def _run(job_id):
    row = db.query("SELECT * FROM jobs WHERE id=?", (job_id,), one=True)
    ctx = JobContext(row)
    db.execute("UPDATE jobs SET status='running', started_at=COALESCE(started_at, ?), updated_at=? WHERE id=?",
               (db.now(), db.now(), job_id))
    db.log("INFO", "jobs", f"{ctx.kind} job {job_id} started", "", ctx.target_id)
    status, error = "done", ""
    try:
        HANDLERS[ctx.kind](ctx)
        if ctx.cancelled:
            status = "cancelled"
    except Exception as exc:            # logged with traceback, never swallowed
        status, error = "failed", f"{type(exc).__name__}: {exc}"
        db.log("ERROR", "jobs", f"{ctx.kind} job {job_id} crashed", traceback.format_exc(), ctx.target_id)
    db.execute("UPDATE jobs SET status=?, error=?, finished_at=?, updated_at=? WHERE id=?",
               (status, error[:500], db.now(), db.now(), job_id))
    db.log("INFO" if status != "failed" else "ERROR", "jobs", f"{ctx.kind} job {job_id} {status}", error, ctx.target_id)


def pause(job_id):
    job = _get(job_id)
    if job["status"] != "running" or not _alive(job_id):
        raise ValueError("Only a running job can be paused")
    _controls[job_id]["pause"].set()
    db.execute("UPDATE jobs SET status='paused', updated_at=? WHERE id=?", (db.now(), job_id))


def resume(job_id):
    job = _get(job_id)
    if job["status"] not in ("paused", "interrupted"):
        raise ValueError("Only a paused or interrupted job can be resumed")
    if _alive(job_id):
        _controls[job_id]["pause"].clear()
        db.execute("UPDATE jobs SET status='running', updated_at=? WHERE id=?", (db.now(), job_id))
    else:                                # no thread (app was restarted): continue from the checkpoint
        _spawn(job_id)


def cancel(job_id):
    job = _get(job_id)
    if job["status"] not in db.JOB_ACTIVE:
        raise ValueError("Job is not active")
    if _alive(job_id):
        _controls[job_id]["cancel"].set()
        db.execute("UPDATE jobs SET message='Cancelling...' WHERE id=?", (job_id,))
    else:
        db.execute("UPDATE jobs SET status='cancelled', finished_at=?, updated_at=? WHERE id=?",
                   (db.now(), db.now(), job_id))


def _get(job_id):
    job = db.query("SELECT * FROM jobs WHERE id=?", (job_id,), one=True)
    if not job:
        raise ValueError("Job not found")
    return job


def recover_on_startup():
    """Mark jobs left 'running' by a previous process as interrupted; auto-resume the safe ones."""
    stale = db.query("SELECT id, kind FROM jobs WHERE status IN ('running','queued')")
    for j in stale:
        db.execute("UPDATE jobs SET status='interrupted', message='Interrupted by restart', updated_at=? WHERE id=?",
                   (db.now(), j["id"]))
    cfg = settings.load()
    resumed = []
    for j in stale:
        if cfg["auto_resume_jobs"] and j["kind"] in SAFE_TO_AUTO_RESUME:
            resume(j["id"])
            resumed.append(j["id"])
    if stale:
        db.log("INFO", "jobs", f"{len(stale)} unfinished job(s) found; auto-resumed: {resumed or 'none'}")
    return stale


# ------------------------------------------------------------------ handlers
def _evaluate_pending(ctx, target, fetcher, cfg, base_progress=0, base_total=0):
    ids = ctx.params.get("ids")
    done = 0
    if ids:
        placeholders = ",".join("?" * len(ids))
        db.execute(f"UPDATE opportunities SET status='new', user_override=0 WHERE target_id=? AND id IN ({placeholders})",
                   [target["id"]] + list(ids))
    while not ctx.cancelled:
        ctx.wait_if_paused()
        if ctx.cancelled:
            break
        opp = db.query("SELECT * FROM opportunities WHERE target_id=? AND status='new' ORDER BY id LIMIT 1",
                       (target["id"],), one=True)
        if not opp:
            break
        remaining = db.query("SELECT COUNT(*) c FROM opportunities WHERE target_id=? AND status='new'",
                             (target["id"],), one=True)["c"]
        ctx.update(base_progress + done, base_total + done + remaining, f"Assessing {opp['domain']}")
        try:
            result = quality.evaluate(opp, target, fetcher, cfg)
            quality.save_assessment(opp, result)
        except Exception as exc:
            db.log("ERROR", "quality", f"Assessment failed for {opp['url']}", traceback.format_exc(), target["id"])
            db.execute("UPDATE opportunities SET status='review', last_error=?, updated_at=? WHERE id=?",
                       (f"Assessment error: {exc}"[:300], db.now(), opp["id"]))
        done += 1
    return done


def discovery(ctx):
    target = db.query("SELECT * FROM targets WHERE id=?", (ctx.target_id,), one=True)
    cfg = settings.load()
    fetcher = crawler.Fetcher(should_stop=ctx.should_stop, cfg=cfg)
    steps = ctx.checkpoint.get("steps")
    if steps is None:
        steps = finder.build_steps(target, ctx.params, cfg)
        ctx.save_checkpoint({"steps": steps, "steps_done": 0})
    done = ctx.checkpoint.get("steps_done", 0)
    total = len(steps)
    if not steps:
        db.log("WARNING", "finder", "No discovery sources are enabled or configured. Import URLs or add a "
               "reference page / search provider.", "", target["id"])
    for i in range(done, total):
        ctx.wait_if_paused()
        if ctx.cancelled:
            return
        step = steps[i]
        ctx.update(i, total, f"Searching: {step['type']} {step.get('name') or step.get('query') or step.get('url', '')}"[:120])
        try:
            finder.run_step(step, target, fetcher, cfg, ctx)
        except Exception:
            db.log("ERROR", "finder", f"Step {i + 1} ({step['type']}) failed", traceback.format_exc(), target["id"])
        ctx.save_checkpoint({"steps_done": i + 1})
    ctx.update(total, total, "Sources searched; assessing opportunities")
    _evaluate_pending(ctx, target, fetcher, cfg, total, total)


def evaluate(ctx):
    target = db.query("SELECT * FROM targets WHERE id=?", (ctx.target_id,), one=True)
    cfg = settings.load()
    _evaluate_pending(ctx, target, crawler.Fetcher(should_stop=ctx.should_stop, cfg=cfg), cfg)


def submit(ctx):
    target = db.query("SELECT * FROM targets WHERE id=?", (ctx.target_id,), one=True)
    cfg = settings.load()
    fetcher = crawler.Fetcher(should_stop=ctx.should_stop, cfg=cfg)
    ids = ctx.params.get("ids")
    sql = "SELECT id, domain FROM opportunities WHERE target_id=? AND status='eligible'"
    params = [ctx.target_id]
    if cfg["require_approval"]:
        sql += " AND approved=1"
    if ids:
        sql += f" AND id IN ({','.join('?' * len(ids))})"
        params += list(ids)
    sql += (" AND id NOT IN (SELECT opportunity_id FROM submissions WHERE status IN "
            "('submitted','awaiting_approval','pending_verification','already_submitted'))"
            " AND (SELECT COUNT(*) FROM submissions s WHERE s.opportunity_id=opportunities.id AND s.method='auto') < ?"
            " ORDER BY id")
    params.append(cfg["max_submission_attempts"])
    queue = db.query(sql, params)
    already = set(ctx.checkpoint.get("done_ids", []))
    queue = [q for q in queue if q["id"] not in already]
    total = len(queue) + len(already)
    if not queue:
        db.log("INFO", "submitter", "Nothing to submit: no approved, eligible opportunities", "", ctx.target_id)
    done_ids = list(already)
    conc = cfg["submission_concurrency"]
    i = 0
    while i < len(queue) and not ctx.cancelled:
        ctx.wait_if_paused()
        if ctx.cancelled:
            break
        batch = queue[i:i + conc]
        threads, results = [], {}

        def work(item):
            try:
                results[item["id"]] = submitter.submit(item["id"], fetcher, cfg)
            except Exception:
                db.log("ERROR", "submitter", f"Submission crashed for {item['domain']}", traceback.format_exc(),
                       ctx.target_id)
        for item in batch:
            t = threading.Thread(target=work, args=(item,), daemon=True)
            t.start()
            threads.append(t)
        for t in threads:
            t.join()
        for item in batch:
            done_ids.append(item["id"])
        i += len(batch)
        ctx.save_checkpoint({"done_ids": done_ids})
        ctx.update(len(done_ids), total, f"Processed {len(done_ids)} of {total}")


def verify(ctx):
    cfg = settings.load()
    fetcher = crawler.Fetcher(should_stop=ctx.should_stop, cfg=cfg)
    ids = ctx.checkpoint.get("ids")
    if ids is None:
        ids = checker.due_ids(ctx.target_id, force=bool(ctx.params.get("force")))
        ctx.save_checkpoint({"ids": ids, "index": 0})
    start = ctx.checkpoint.get("index", 0)
    for i in range(start, len(ids)):
        ctx.wait_if_paused()
        if ctx.cancelled:
            return
        ctx.update(i, len(ids), f"Checking {i + 1} of {len(ids)}")
        try:
            checker.run_check(ids[i], fetcher, cfg)
        except Exception:
            db.log("ERROR", "checker", f"Check failed for backlink {ids[i]}", traceback.format_exc(), ctx.target_id)
        ctx.save_checkpoint({"index": i + 1})
    ctx.update(len(ids), len(ids), "Verification finished" if ids else "Nothing due for checking")


HANDLERS = {"discovery": discovery, "evaluate": evaluate, "submit": submit, "verify": verify}
