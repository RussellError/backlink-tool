"""SQLite access. One short-lived connection per call, one process-wide write lock.

journal_mode is left at the default (DELETE) on purpose: WAL does not work on
Android shared storage (/storage/emulated/0), which is where the project may live.
Existing data is never dropped; the schema uses CREATE ... IF NOT EXISTS.
"""
import json
import logging
import os
import sqlite3
import threading
from collections import namedtuple
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.environ.get("BACKLINK_DB", os.path.join(BASE_DIR, "database", "backlinks.db"))
_write_lock = threading.RLock()
logger = logging.getLogger("backlink")
Result = namedtuple("Result", "lastrowid rowcount")

# Opportunity classification (the six buckets from the workflow + "new" = not yet assessed)
OPP_STATUSES = ("new", "eligible", "review", "login", "captcha", "outreach", "rejected")
SUBMISSION_STATUSES = ("submitted", "awaiting_approval", "pending_verification", "captcha_required",
                       "login_required", "manual_action", "rejected", "failed", "already_submitted")
BACKLINK_STATES = ("pending", "live", "live_nofollow", "live_sponsored", "live_ugc", "redirected",
                   "not_found", "access_restricted", "temporary_failure", "inconclusive")
LIVE_STATES = ("live", "live_nofollow", "live_sponsored", "live_ugc")
JOB_ACTIVE = ("queued", "running", "paused", "interrupted")


def now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def hours_from_now(hours):
    return (datetime.now(timezone.utc) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sql_in(values):
    return "(" + ",".join("'%s'" % v for v in values) + ")"


SCHEMA = f"""
CREATE TABLE IF NOT EXISTS targets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT NOT NULL UNIQUE,
  domain TEXT NOT NULL,
  name TEXT NOT NULL DEFAULT '',
  description TEXT NOT NULL DEFAULT '',
  category TEXT NOT NULL DEFAULT '',
  keywords TEXT NOT NULL DEFAULT '',
  languages TEXT NOT NULL DEFAULT '',
  countries TEXT NOT NULL DEFAULT '',
  contact_email TEXT NOT NULL DEFAULT '',
  opp_types TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS opportunities (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  url TEXT NOT NULL,
  domain TEXT NOT NULL,
  category TEXT NOT NULL,
  source TEXT NOT NULL DEFAULT 'manual',
  title TEXT NOT NULL DEFAULT '',
  notes TEXT NOT NULL DEFAULT '',
  hint_status TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new' CHECK (status IN {_sql_in(OPP_STATUSES)}),
  approved INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0,1)),
  user_override INTEGER NOT NULL DEFAULT 0 CHECK (user_override IN (0,1)),
  submit_url TEXT NOT NULL DEFAULT '',
  last_error TEXT NOT NULL DEFAULT '',
  discovered_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE (target_id, url)
);
CREATE INDEX IF NOT EXISTS idx_opp_target_status ON opportunities(target_id, status);
CREATE INDEX IF NOT EXISTS idx_opp_domain ON opportunities(target_id, domain);
CREATE TABLE IF NOT EXISTS assessments (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_id INTEGER NOT NULL UNIQUE REFERENCES opportunities(id) ON DELETE CASCADE,
  score INTEGER NOT NULL DEFAULT 0,
  summary TEXT NOT NULL DEFAULT '',
  verified TEXT NOT NULL DEFAULT '{{}}',
  estimated TEXT NOT NULL DEFAULT '{{}}',
  breakdown TEXT NOT NULL DEFAULT '[]',
  flags TEXT NOT NULL DEFAULT '[]',
  http_status INTEGER,
  final_url TEXT NOT NULL DEFAULT '',
  assessed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  status TEXT NOT NULL CHECK (status IN {_sql_in(SUBMISSION_STATUSES)}),
  method TEXT NOT NULL DEFAULT 'auto' CHECK (method IN ('auto','manual')),
  destination_url TEXT NOT NULL DEFAULT '',
  http_status INTEGER,
  detail TEXT NOT NULL DEFAULT '',
  payload TEXT NOT NULL DEFAULT '{{}}',
  listing_url TEXT NOT NULL DEFAULT '',
  submitted_at TEXT NOT NULL,
  verify_after TEXT
);
CREATE INDEX IF NOT EXISTS idx_sub_opp ON submissions(opportunity_id);
CREATE TABLE IF NOT EXISTS backlinks (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  opportunity_id INTEGER REFERENCES opportunities(id) ON DELETE SET NULL,
  submission_id INTEGER REFERENCES submissions(id) ON DELETE SET NULL,
  source_url TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'pending' CHECK (state IN {_sql_in(BACKLINK_STATES)}),
  final_url TEXT NOT NULL DEFAULT '',
  http_status INTEGER,
  href TEXT NOT NULL DEFAULT '',
  anchor_text TEXT NOT NULL DEFAULT '',
  rel TEXT NOT NULL DEFAULT '',
  link_count INTEGER NOT NULL DEFAULT 0,
  first_seen TEXT,
  last_checked TEXT,
  next_check TEXT,
  check_count INTEGER NOT NULL DEFAULT 0,
  consecutive_failures INTEGER NOT NULL DEFAULT 0,
  disappeared INTEGER NOT NULL DEFAULT 0,
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  UNIQUE (target_id, source_url)
);
CREATE TABLE IF NOT EXISTS verification_history (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  backlink_id INTEGER NOT NULL REFERENCES backlinks(id) ON DELETE CASCADE,
  checked_at TEXT NOT NULL,
  state TEXT NOT NULL,
  http_status INTEGER,
  final_url TEXT NOT NULL DEFAULT '',
  anchor_text TEXT NOT NULL DEFAULT '',
  rel TEXT NOT NULL DEFAULT '',
  error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS outreach_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  opportunity_id INTEGER NOT NULL REFERENCES opportunities(id) ON DELETE CASCADE,
  subject TEXT NOT NULL,
  body TEXT NOT NULL,
  status TEXT NOT NULL DEFAULT 'draft',
  created_at TEXT NOT NULL,
  UNIQUE (opportunity_id, body)
);
CREATE TABLE IF NOT EXISTS jobs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  target_id INTEGER NOT NULL REFERENCES targets(id) ON DELETE CASCADE,
  kind TEXT NOT NULL CHECK (kind IN ('discovery','evaluate','submit','verify')),
  status TEXT NOT NULL CHECK (status IN ('queued','running','paused','interrupted','cancelled','done','failed')),
  progress INTEGER NOT NULL DEFAULT 0,
  total INTEGER NOT NULL DEFAULT 0,
  message TEXT NOT NULL DEFAULT '',
  params TEXT NOT NULL DEFAULT '{{}}',
  checkpoint TEXT NOT NULL DEFAULT '{{}}',
  error TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  started_at TEXT,
  updated_at TEXT NOT NULL,
  finished_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_one_active
  ON jobs(target_id, kind) WHERE status IN {_sql_in(JOB_ACTIVE)};
CREATE TABLE IF NOT EXISTS logs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT NOT NULL,
  level TEXT NOT NULL,
  source TEXT NOT NULL,
  message TEXT NOT NULL,
  detail TEXT NOT NULL DEFAULT '',
  target_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(id DESC);
"""


def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    with _write_lock:
        conn = connect()
        try:
            conn.executescript(SCHEMA)
            conn.commit()
        finally:
            conn.close()


def query(sql, params=(), one=False):
    conn = connect()
    try:
        rows = [dict(r) for r in conn.execute(sql, params).fetchall()]
    finally:
        conn.close()
    if one:
        return rows[0] if rows else None
    return rows


def execute(sql, params=()):
    with _write_lock:
        conn = connect()
        try:
            cur = conn.execute(sql, params)
            conn.commit()
            return Result(cur.lastrowid, cur.rowcount)
        finally:
            conn.close()


@contextmanager
def transaction():
    with _write_lock:
        conn = connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()


def log(level, source, message, detail="", target_id=None):
    """Persist a log line. Never raises: logging must not break a running job."""
    try:
        getattr(logger, level.lower() if level.lower() in ("debug", "info", "warning", "error") else "info")(
            "[%s] %s %s", source, message, detail or "")
        execute("INSERT INTO logs (ts, level, source, message, detail, target_id) VALUES (?,?,?,?,?,?)",
                (now(), level.upper(), source, str(message)[:500], str(detail)[:4000], target_id))
    except Exception as exc:  # pragma: no cover - last-resort fallback
        logger.error("could not write log entry: %s (%s)", message, exc)


def dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(text, default=None):
    try:
        return json.loads(text) if text else default
    except (TypeError, ValueError):
        return default
