"""Settings stored in config.json. Only whitelisted keys are accepted.

No secrets are stored here. Optional API tokens (e.g. GITHUB_TOKEN) are read
from environment variables so they never touch the disk.
"""
import json
import os
import threading

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.environ.get("BACKLINK_CONFIG", os.path.join(BASE_DIR, "config.json"))
_lock = threading.Lock()

# key: (default, type, min, max)  -- min/max are value bounds for numbers,
# length bounds for strings.
SCHEMA = {
    "port": (8080, int, 1024, 65535),
    "user_agent": ("BacklinkBuilderBot/1.0 (personal local SEO tool)", str, 10, 200),
    "request_timeout": (15, int, 3, 60),
    "retry_limit": (2, int, 0, 5),
    "retry_delay": (3, float, 0.0, 60),
    "per_domain_delay": (2.0, float, 0.0, 60),
    "max_redirects": (5, int, 0, 10),
    "max_response_kb": (1536, int, 64, 8192),
    "submission_concurrency": (1, int, 1, 3),
    "max_candidates_per_run": (150, int, 10, 1000),
    "auto_resume_jobs": (True, bool, None, None),
    "require_approval": (True, bool, None, None),
    "verify_delay_hours": (24, int, 0, 720),
    "recheck_interval_hours": (72, int, 1, 2160),
    "max_submission_attempts": (3, int, 1, 10),
    "min_eligible_score": (40, int, 0, 100),
    "enable_seed_sources": (True, bool, None, None),
    "enable_github_search": (True, bool, None, None),
    "searxng_url": ("", str, 0, 300),
    "searxng_max_queries": (8, int, 0, 40),
    "broken_link_scan": (True, bool, None, None),
    "broken_link_max_checks": (15, int, 0, 50),
}
DEFAULTS = {k: v[0] for k, v in SCHEMA.items()}


class SettingsError(ValueError):
    pass


def _coerce(key, value):
    default, typ, lo, hi = SCHEMA[key]
    if typ is bool:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            return value.lower() in ("true", "1", "yes", "on")
        raise SettingsError(f"{key}: must be true or false")
    if typ in (int, float):
        try:
            num = typ(value)
        except (TypeError, ValueError):
            raise SettingsError(f"{key}: must be a number")
        if num < lo or num > hi:
            raise SettingsError(f"{key}: must be between {lo} and {hi}")
        return num
    text = str(value if value is not None else "").strip()
    if len(text) < lo or len(text) > hi:
        raise SettingsError(f"{key}: length must be between {lo} and {hi} characters")
    if key == "searxng_url" and text:
        from . import safety  # lazy import: avoids a circular import at load time
        try:
            text = safety.validate_url(text, resolve=False)
        except safety.UnsafeURL as exc:
            raise SettingsError(f"{key}: {exc}")
    if key == "user_agent" and any(ord(c) < 32 for c in text):
        raise SettingsError("user_agent: control characters are not allowed")
    return text


def load():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
        if isinstance(raw, dict):
            for key, value in raw.items():
                if key in SCHEMA:
                    try:
                        cfg[key] = _coerce(key, value)
                    except SettingsError:
                        pass  # invalid stored value -> keep the safe default
    except FileNotFoundError:
        pass
    except json.JSONDecodeError as exc:
        print(f"[settings] config.json is not valid JSON ({exc}); using defaults")
    return cfg


def update(changes):
    if not isinstance(changes, dict):
        raise SettingsError("Settings must be an object")
    cfg = load()
    for key, value in changes.items():
        if key not in SCHEMA:
            raise SettingsError(f"Unknown setting: {key}")
        cfg[key] = _coerce(key, value)
    with _lock:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(cfg, fh, indent=2)
        os.replace(tmp, CONFIG_PATH)
    return cfg


def provider_status():
    """Which discovery sources are usable right now."""
    cfg = load()
    return {
        "seed_sources": cfg["enable_seed_sources"],
        "github_search": cfg["enable_github_search"],
        "github_token_present": bool(os.environ.get("GITHUB_TOKEN")),
        "searxng": bool(cfg["searxng_url"]),
    }
