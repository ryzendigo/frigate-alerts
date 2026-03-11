"""
Frigate Alerts - Main Application (v2.4 - Hardened for reliability)
Animated GIF notifications from Frigate NVR events.
Supports: Pushover, Discord, Telegram, Ntfy, Gotify, Email, Webhook

Architecture:
  - MQTT primary (instant): subscribes to frigate/reviews, fires on "new"
  - MQTT "update" handler: catches late object detection (e.g., car->person)
  - API polling fallback: catches anything MQTT misses
  - Two-phase notifications:
      Phase 1: Instant snapshot on event creation (<2s target)
      Phase 2: GIF upgrade when event ends (or after timeout)
  - Phase 2 context persisted to SQLite (survives container restarts)
  - Event queue with SQLite persistence for guaranteed delivery
  - Separate thread pools: event_pool (processing) + provider_pool (dispatch)
  - Auto-reconnecting MQTT with exponential backoff
  - Provider-level retry with return-value inspection for transient failures
  - Circuit breaker for Frigate API (backs off after repeated failures)
  - Connection-pooled HTTP sessions for notifier providers
"""

import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path

import requests
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .notifiers.pushover import send_pushover
from .notifiers.discord import send_discord, update_discord
from .notifiers.telegram import send_telegram
from .notifiers.ntfy import send_ntfy
from .notifiers.gotify import send_gotify
from .notifiers.smtp import send_smtp
from .notifiers.webhook import send_webhook

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("frigate-alerts")

CONFIG_PATH = os.environ.get("CONFIG_PATH", "/app/config/config.yml")
DB_PATH = os.environ.get("DB_PATH", "/app/config/alerts.db")

config = {}
mqtt_client = None
mqtt_connected = False
poller_running = False
shutting_down = threading.Event()

# Thread-safe dedup using OrderedDict for recency-based trimming
_dedup_lock = threading.Lock()
notified_reviews = OrderedDict()  # review_id -> timestamp (reviews we sent notifications for)
seen_reviews = OrderedDict()       # review_id -> timestamp (reviews we've seen but filtered)

# Phase 2 tracking: review_id -> {msg_refs, title, message, ...}
_phase2_lock = threading.Lock()
pending_phase2 = {}

# Thread-safe cooldown and stats
_cooldown_lock = threading.Lock()
camera_cooldowns = {}

_stats_lock = threading.Lock()
stats = {"phase1_sent": 0, "phase2_sent": 0, "events_received": 0, "events_skipped": 0, "errors": 0}

# Snooze: timestamp when snooze expires (0 = not snoozed)
snooze_until = 0

# SEPARATE worker pools to prevent deadlock:
# event_pool: processes events (Phase 1 + Phase 2). Each event blocks waiting for providers.
# provider_pool: dispatches to notification providers. Never blocks on event_pool.
event_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="event")
provider_pool = ThreadPoolExecutor(max_workers=16, thread_name_prefix="provider")

# Thread-local HTTP sessions (requests.Session is NOT thread-safe)
_thread_local = threading.local()

# Poller error suppression
_last_poll_error = ""
_poll_error_count = 0

# Circuit breaker for Frigate API
_frigate_cb_lock = threading.Lock()
_frigate_consecutive_failures = 0
_frigate_circuit_open_until = 0  # timestamp when circuit closes
FRIGATE_CB_THRESHOLD = 5  # failures before opening circuit
FRIGATE_CB_COOLDOWN = 30  # seconds to wait before retrying


def _get_session():
    """Get a thread-local requests.Session with connection pooling."""
    if not hasattr(_thread_local, "session") or _thread_local.session is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=8, max_retries=0
        )
        s.mount("http://", adapter)
        s.mount("https://", adapter)
        _thread_local.session = s
    return _thread_local.session


def _get_notifier_session():
    """Get a thread-local session for external notification APIs (Pushover, Discord, etc.)."""
    if not hasattr(_thread_local, "notifier_session") or _thread_local.notifier_session is None:
        s = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=4, pool_maxsize=8, max_retries=0
        )
        s.mount("https://", adapter)
        _thread_local.notifier_session = s
    return _thread_local.notifier_session


def _frigate_circuit_ok():
    """Check if Frigate circuit breaker allows a request."""
    with _frigate_cb_lock:
        if _frigate_consecutive_failures < FRIGATE_CB_THRESHOLD:
            return True
        return time.time() >= _frigate_circuit_open_until


def _frigate_success():
    """Record a successful Frigate API call, closing the circuit."""
    global _frigate_consecutive_failures, _frigate_circuit_open_until
    with _frigate_cb_lock:
        if _frigate_consecutive_failures > 0:
            log.info("Frigate circuit breaker: recovered after %d failures", _frigate_consecutive_failures)
        _frigate_consecutive_failures = 0
        _frigate_circuit_open_until = 0


def _frigate_failure():
    """Record a failed Frigate API call. Opens circuit after threshold."""
    global _frigate_consecutive_failures, _frigate_circuit_open_until
    with _frigate_cb_lock:
        _frigate_consecutive_failures += 1
        if _frigate_consecutive_failures >= FRIGATE_CB_THRESHOLD:
            _frigate_circuit_open_until = time.time() + FRIGATE_CB_COOLDOWN
            if _frigate_consecutive_failures == FRIGATE_CB_THRESHOLD:
                log.warning("Frigate circuit breaker OPEN: %d consecutive failures, backing off %ds",
                           _frigate_consecutive_failures, FRIGATE_CB_COOLDOWN)


def _inc_stat(key, value=1):
    with _stats_lock:
        stats[key] = stats.get(key, 0) + value


def _add_to_seen(review_id):
    """Track a review as seen (filtered/stale). Trims oldest entries when full."""
    with _dedup_lock:
        seen_reviews[review_id] = time.time()
        if len(seen_reviews) > 3000:
            for _ in range(1000):
                seen_reviews.popitem(last=False)


def load_config():
    global config
    try:
        with open(CONFIG_PATH) as f:
            loaded = yaml.safe_load(f)
        if not isinstance(loaded, dict):
            log.error("Config file is not a valid YAML dict, using defaults")
            config = {}
        else:
            config = loaded
        _validate_config()
    except FileNotFoundError:
        log.error("Config file not found: %s, using defaults", CONFIG_PATH)
        config = {}
    except yaml.YAMLError as e:
        log.error("Config file has invalid YAML: %s, using defaults", e)
        config = {}
    return config


def _validate_config():
    """Log warnings for missing or invalid config values."""
    if not config.get("frigate", {}).get("url"):
        log.warning("Config: frigate.url not set, using default http://frigate:5000")
    cameras = config.get("cameras", [])
    if not cameras:
        log.warning("Config: no cameras configured — will process ALL cameras")
    labels = config.get("labels", [])
    if not labels:
        log.warning("Config: no labels configured — will process ALL labels")
    if not isinstance(cameras, list):
        log.error("Config: 'cameras' should be a list, got %s", type(cameras).__name__)
        config["cameras"] = []
    if not isinstance(labels, list):
        log.error("Config: 'labels' should be a list, got %s", type(labels).__name__)
        config["labels"] = []
    zones = config.get("zones", {})
    if not isinstance(zones, dict):
        log.error("Config: 'zones' should be a dict, got %s — resetting", type(zones).__name__)
        config["zones"] = {}


def save_config(new_config):
    global config
    try:
        with open(CONFIG_PATH, "w") as f:
            yaml.dump(new_config, f, default_flow_style=False, sort_keys=False)
        config = new_config
    except Exception as e:
        log.error("Failed to save config: %s (in-memory config NOT updated)", e)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

@contextmanager
def get_db():
    """Context manager for SQLite connections. Ensures proper cleanup."""
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        try:
            result = conn.execute("PRAGMA quick_check").fetchone()
            if result and result[0] != "ok":
                log.error("Database integrity check failed: %s", result[0])
        except Exception as e:
            log.error("Database integrity check error: %s", e)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp REAL,
                review_id TEXT,
                event_id TEXT,
                camera TEXT,
                label TEXT,
                zones TEXT,
                provider TEXT,
                recipient TEXT,
                status TEXT,
                message TEXT,
                gif_size INTEGER
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_events (
                review_id TEXT PRIMARY KEY,
                event_data TEXT,
                phase TEXT,
                attempts INTEGER DEFAULT 0,
                next_retry REAL,
                created_at REAL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS pending_phase2 (
                review_id TEXT PRIMARY KEY,
                context_data TEXT,
                created_at REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_history_timestamp ON history(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_pending_next_retry ON pending_events(next_retry)")
        conn.commit()


def log_notification(review_id, event_id, camera, label, zones, provider, recipient, status, message="", gif_size=0):
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT INTO history (timestamp, review_id, event_id, camera, label, zones, provider, recipient, status, message, gif_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (time.time(), review_id, event_id, camera, label, zones, provider, recipient, status, message, gif_size),
            )
            conn.commit()
    except Exception as e:
        log.error("DB log error: %s", e)


def get_history(limit=50):
    try:
        with get_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT * FROM history ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def save_pending_event(review_id, event_data, phase):
    try:
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending_events (review_id, event_data, phase, attempts, next_retry, created_at) VALUES (?, ?, ?, 0, ?, ?)",
                (review_id, json.dumps(event_data), phase, time.time(), time.time()),
            )
            conn.commit()
    except Exception as e:
        log.error("Save pending event error: %s", e)


def remove_pending_event(review_id):
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM pending_events WHERE review_id = ?", (review_id,))
            conn.commit()
    except Exception as e:
        log.error("Remove pending event error: %s", e)


def get_pending_events():
    try:
        with get_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM pending_events WHERE next_retry <= ? AND attempts < 5 ORDER BY created_at",
                (time.time(),),
            ).fetchall()
            return [dict(r) for r in rows]
    except Exception:
        return []


def increment_pending_retry(review_id, attempts):
    """Exponential backoff: 30s, 60s, 120s, 240s, 480s"""
    backoff = min(30 * (2 ** attempts), 600)
    try:
        with get_db() as conn:
            conn.execute(
                "UPDATE pending_events SET attempts = attempts + 1, next_retry = ? WHERE review_id = ?",
                (time.time() + backoff, review_id),
            )
            conn.commit()
    except Exception as e:
        log.error("Increment retry error: %s", e)


# Phase 2 persistence — survives container restarts
def save_phase2_context(review_id, ctx):
    """Persist Phase 2 context to SQLite so GIF upgrades survive restarts."""
    try:
        # msg_refs contains config dicts with tokens — only persist what we need
        serializable_ctx = {
            "title": ctx["title"],
            "message": ctx["message"],
            "frigate_url": ctx["frigate_url"],
            "video_url": ctx["video_url"],
            "event_id": ctx["event_id"],
            "camera": ctx["camera"],
            "label_str": ctx["label_str"],
            "zone_str": ctx["zone_str"],
            "created_at": ctx["created_at"],
            # Persist discord message refs for editing
            "discord_refs": ctx.get("msg_refs", {}).get("discord", []),
            # Persist pushover recipient info for silent follow-up
            "pushover_recipients": [
                {"name": ref["recipient"].get("name", ""), "userkey": ref["recipient"].get("userkey", "")}
                for ref in ctx.get("msg_refs", {}).get("pushover", [])
            ],
        }
        with get_db() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO pending_phase2 (review_id, context_data, created_at) VALUES (?, ?, ?)",
                (review_id, json.dumps(serializable_ctx), ctx["created_at"]),
            )
            conn.commit()
    except Exception as e:
        log.error("Save Phase 2 context error: %s", e)


def remove_phase2_context(review_id):
    """Remove Phase 2 context from SQLite after successful GIF send."""
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM pending_phase2 WHERE review_id = ?", (review_id,))
            conn.commit()
    except Exception as e:
        log.error("Remove Phase 2 context error: %s", e)


def load_pending_phase2():
    """Load Phase 2 contexts from SQLite on startup. Rebuilds msg_refs from config."""
    try:
        with get_db() as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM pending_phase2 WHERE created_at > ?",
                (time.time() - 600,),  # Only load contexts < 10 min old
            ).fetchall()
            count = 0
            for row in rows:
                try:
                    ctx = json.loads(row["context_data"])
                    review_id = row["review_id"]

                    # Rebuild msg_refs from persisted data + current config
                    msg_refs = {}

                    # Rebuild discord refs (webhook_url + message_id are all we need)
                    discord_refs = ctx.get("discord_refs", [])
                    if discord_refs:
                        msg_refs["discord"] = discord_refs

                    # Rebuild pushover refs from current config + persisted recipients
                    po = config.get("pushover", {})
                    if po.get("enabled"):
                        po_refs = []
                        persisted_recipients = {r["userkey"]: r for r in ctx.get("pushover_recipients", [])}
                        for r in po.get("recipients", []):
                            if r.get("userkey") in persisted_recipients:
                                cam_overrides = {}
                                for co in po.get("camera_overrides", []):
                                    if co.get("camera") == ctx.get("camera"):
                                        cam_overrides = co
                                        break
                                po_refs.append({"config": po, "recipient": r, "camera_overrides": cam_overrides})
                        if po_refs:
                            msg_refs["pushover"] = po_refs

                    ctx["msg_refs"] = msg_refs
                    with _phase2_lock:
                        pending_phase2[review_id] = ctx
                    count += 1
                except (json.JSONDecodeError, KeyError) as e:
                    log.warning("Skipping corrupt Phase 2 entry %s: %s", row["review_id"], e)
            if count:
                log.info("Restored %d pending Phase 2 entries from database", count)
    except Exception as e:
        log.error("Load pending Phase 2 error: %s", e)


def cleanup_phase2_db():
    """Remove stale Phase 2 entries from SQLite."""
    try:
        with get_db() as conn:
            conn.execute("DELETE FROM pending_phase2 WHERE created_at < ?", (time.time() - 600,))
            conn.commit()
    except Exception as e:
        log.error("Cleanup Phase 2 DB error: %s", e)


def cleanup_history():
    retention_days = config.get("history_retention_days", 30)
    cutoff = time.time() - (retention_days * 86400)
    try:
        with get_db() as conn:
            result = conn.execute("DELETE FROM history WHERE timestamp < ?", (cutoff,))
            deleted = result.rowcount
            conn.execute("DELETE FROM pending_events WHERE created_at < ?", (time.time() - 7200,))
            conn.commit()
            if deleted > 0:
                log.info("Cleaned up %d history entries older than %d days", deleted, retention_days)
    except Exception as e:
        log.error("History cleanup error: %s", e)


def run_cleanup_loop():
    while not shutting_down.is_set():
        cleanup_history()
        cleanup_phase2_db()
        with _dedup_lock:
            if len(notified_reviews) > 2000:
                for _ in range(1000):
                    notified_reviews.popitem(last=False)
            if len(seen_reviews) > 3000:
                for _ in range(1000):
                    seen_reviews.popitem(last=False)
        with _phase2_lock:
            stale = [rid for rid, ctx in pending_phase2.items()
                     if time.time() - ctx.get("created_at", 0) > 600]
            for rid in stale:
                pending_phase2.pop(rid, None)
                remove_phase2_context(rid)
            if stale:
                log.info("Cleaned %d stale pending_phase2 entries", len(stale))
        for _ in range(6 * 3600):
            if shutting_down.is_set():
                return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Filtering: cameras, labels, zones, cooldown, quiet hours, snooze
# ---------------------------------------------------------------------------

def is_snoozed():
    return time.time() < snooze_until


def in_quiet_hours():
    quiet = config.get("quiet_hours", {})
    if not quiet.get("enabled"):
        return False
    start = quiet.get("start", "")
    end = quiet.get("end", "")
    if not start or not end:
        return False
    try:
        now = datetime.now().strftime("%H:%M")
        if start <= end:
            return start <= now < end
        else:
            return now >= start or now < end
    except Exception:
        return False


def check_cooldown(camera):
    cooldown_secs = config.get("cooldown", 0)
    if cooldown_secs <= 0:
        return False
    with _cooldown_lock:
        last = camera_cooldowns.get(camera, 0)
    return time.time() - last < cooldown_secs


def set_cooldown(camera):
    with _cooldown_lock:
        camera_cooldowns[camera] = time.time()


def check_zones(event_zones):
    zone_cfg = config.get("zones", {})
    allow = zone_cfg.get("allow", [])
    block = zone_cfg.get("block", [])
    if block and any(z in block for z in event_zones):
        return False
    if allow and not any(z in allow for z in event_zones):
        return False
    return True


def should_notify(camera, labels, zones):
    allowed_cameras = config.get("cameras", [])
    if allowed_cameras and camera not in allowed_cameras:
        return False
    allowed_labels = config.get("labels", [])
    if allowed_labels and not any(l in allowed_labels for l in labels):
        return False
    if not check_zones(zones):
        return False
    return True


def filter_labels(objects):
    """Return only the labels that match the configured allowed labels.
    If no label filter is configured, return all objects."""
    allowed_labels = config.get("labels", [])
    if not allowed_labels:
        return objects
    return [o for o in objects if o in allowed_labels]


# ---------------------------------------------------------------------------
# Message template rendering
# ---------------------------------------------------------------------------

def render_template(template, variables):
    try:
        return template.format(**variables)
    except (KeyError, ValueError, IndexError):
        return template


def build_message(camera, label_str, zone_str, review_id, event_id):
    variables = {
        "camera": camera,
        "label": label_str.title(),
        "labels": label_str,
        "zone": zone_str,
        "zones": zone_str,
        "time": datetime.now().strftime("%H:%M:%S"),
        "date": datetime.now().strftime("%Y-%m-%d"),
        "review_id": review_id,
        "event_id": event_id,
    }
    title_tpl = config.get("title_template", "{label} - {camera}")
    msg_tpl = config.get("message_template", "")
    title = render_template(title_tpl, variables)
    if msg_tpl:
        message = render_template(msg_tpl, variables)
    else:
        message = f"{label_str.title()} detected on {camera}"
        if zone_str:
            message += f" ({zone_str})"
    return title, message


# ---------------------------------------------------------------------------
# Media fetching (with circuit breaker)
# ---------------------------------------------------------------------------

# Max clip size to process (50MB) — prevents ffmpeg from consuming excessive memory
MAX_CLIP_SIZE = 50 * 1024 * 1024

def clip_to_gif(clip_data, max_duration=3, fps=6, width=240):
    if len(clip_data) > MAX_CLIP_SIZE:
        log.warning("Clip too large (%d bytes > %d), skipping GIF conversion", len(clip_data), MAX_CLIP_SIZE)
        return None

    mp4_path = None
    gif_path = None
    proc = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as mp4:
            mp4.write(clip_data)
            mp4_path = mp4.name
        gif_path = mp4_path.replace(".mp4", ".gif")
        cmd = [
            "nice", "-n", "19",  # Low CPU priority
            "ffmpeg", "-y", "-i", mp4_path,
            "-t", str(max_duration),
            "-threads", "2",  # Limit ffmpeg threads
            "-vf", f"fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer",
            "-loop", "0",
            gif_path,
        ]
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            _, stderr = proc.communicate(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            log.warning("ffmpeg GIF conversion timed out (20s), process killed")
            return None
        if proc.returncode == 0 and os.path.exists(gif_path):
            with open(gif_path, "rb") as f:
                gif_data = f.read()
            if len(gif_data) > 100:
                log.info("Generated GIF from clip (%d bytes)", len(gif_data))
                return gif_data
        else:
            stderr_str = stderr[-200:].decode("utf-8", errors="replace") if stderr else "unknown"
            log.warning("ffmpeg GIF conversion failed: %s", stderr_str)
    except Exception as e:
        log.warning("GIF conversion error: %s", e)
        if proc and proc.poll() is None:
            try:
                proc.kill()
                proc.wait()
            except Exception:
                pass
    finally:
        for p in (mp4_path, gif_path):
            if p:
                try:
                    os.unlink(p)
                except OSError:
                    pass
    return None


def fetch_snapshot(event_id):
    if not _frigate_circuit_ok():
        return None, None, None

    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    session = _get_session()
    for attempt in range(3):
        try:
            resp = session.get(f"{frigate_url}/api/events/{event_id}/snapshot.jpg", timeout=5)
            if resp.status_code == 200 and len(resp.content) > 100:
                _frigate_success()
                log.info("Fetched snapshot for %s (%d bytes)", event_id, len(resp.content))
                return resp.content, "image/jpeg", "jpg"
            if resp.status_code == 404 and attempt < 2:
                time.sleep(0.5)
                continue
            _frigate_failure()
        except requests.exceptions.ConnectionError:
            _frigate_failure()
            log.warning("Snapshot fetch: Frigate connection refused (attempt %d)", attempt + 1)
            if attempt < 2:
                time.sleep(1)
        except requests.exceptions.Timeout:
            _frigate_failure()
            log.warning("Snapshot fetch timeout (attempt %d)", attempt + 1)
        except Exception as e:
            _frigate_failure()
            log.error("Snapshot fetch failed (attempt %d): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(0.5)
    return None, None, None


def fetch_gif(event_id, retries=None, retry_interval=None):
    if not _frigate_circuit_ok():
        return None, None, None

    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    retries = retries or config.get("gif_retries", 3)
    retry_interval = retry_interval or config.get("gif_retry_interval", 5)
    session = _get_session()

    for attempt in range(1, retries + 1):
        try:
            resp = session.get(f"{frigate_url}/api/events/{event_id}/clip.mp4", timeout=30)
            if resp.status_code == 200 and len(resp.content) > 1000:
                _frigate_success()
                gif_data = clip_to_gif(resp.content)
                if gif_data:
                    return gif_data, "image/gif", "gif"
            else:
                _frigate_failure()
            log.warning("Clip not ready for %s (attempt %d/%d, status=%d, size=%d)",
                       event_id, attempt, retries, resp.status_code, len(resp.content))
        except requests.exceptions.ConnectionError:
            _frigate_failure()
            log.warning("Clip fetch: Frigate connection refused (attempt %d/%d)", attempt, retries)
        except requests.exceptions.Timeout:
            _frigate_failure()
            log.warning("Clip fetch timeout (attempt %d/%d)", attempt, retries)
        except Exception as e:
            _frigate_failure()
            log.warning("Clip fetch error (attempt %d/%d): %s", attempt, retries, e)
        if attempt < retries:
            time.sleep(retry_interval)

    # Fallback to Frigate's preview.gif
    try:
        resp = session.get(f"{frigate_url}/api/events/{event_id}/preview.gif", timeout=15)
        if resp.status_code == 200 and len(resp.content) > 100:
            _frigate_success()
            log.info("Fell back to Frigate preview.gif for %s (%d bytes)", event_id, len(resp.content))
            return resp.content, "image/gif", "gif"
        _frigate_failure()
    except Exception as e:
        _frigate_failure()
        log.warning("Preview GIF fallback error: %s", e)

    return None, None, None


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------

def build_event_urls(review_id, event_id):
    frigate_public = config.get("frigate", {}).get("public_url", "")
    alerts_public = config.get("alerts_public_url", "")
    frigate_url = f"{frigate_public}/review?id={review_id}" if frigate_public else ""
    video_url = f"{alerts_public}/event/{event_id}" if alerts_public else ""
    return frigate_url, video_url


def build_snooze_url(minutes):
    alerts_public = config.get("alerts_public_url", "")
    if alerts_public:
        return f"{alerts_public}/api/snooze/quick?minutes={minutes}"
    return ""


# ---------------------------------------------------------------------------
# Provider retry wrapper
# ---------------------------------------------------------------------------

def _is_error_result(result):
    """Check if a provider return value indicates a transient error worth retrying."""
    if isinstance(result, str):
        return result.startswith("error:")
    if isinstance(result, tuple) and result:
        return isinstance(result[0], str) and result[0].startswith("error:")
    return False


def _retry_provider(func, provider_name, max_retries=2):
    """Retry a provider send function on transient failures.
    Handles both raised exceptions AND error return values from notifiers."""
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            result = func()
            if _is_error_result(result):
                last_error = result
                if attempt < max_retries:
                    delay = 1 * (2 ** attempt)
                    log.warning("%s returned error (attempt %d/%d): %s, retrying in %ds",
                               provider_name, attempt + 1, max_retries + 1, result, delay)
                    time.sleep(delay)
                    continue
                else:
                    log.warning("%s returned error after %d attempts: %s",
                               provider_name, max_retries + 1, result)
                    return result
            return result
        except requests.exceptions.ConnectionError as e:
            last_error = e
            if attempt < max_retries:
                delay = 1 * (2 ** attempt)
                log.warning("%s connection error (attempt %d/%d), retrying in %ds",
                           provider_name, attempt + 1, max_retries + 1, delay)
                time.sleep(delay)
            else:
                raise
        except requests.exceptions.Timeout as e:
            last_error = e
            if attempt < max_retries:
                log.warning("%s timeout (attempt %d/%d), retrying",
                           provider_name, attempt + 1, max_retries + 1)
                time.sleep(1)
            else:
                raise
        except Exception:
            raise
    if isinstance(last_error, Exception):
        raise last_error
    return last_error


# ---------------------------------------------------------------------------
# Notification dispatch (parallel via provider_pool)
# ---------------------------------------------------------------------------

def send_to_all_providers(title, message, media_data, media_type, media_ext,
                          frigate_url, video_url, review_id, event_id,
                          camera, label_str, zone_str):
    """Send notification to all enabled providers IN PARALLEL using provider_pool."""
    snooze_url = build_snooze_url(30)
    media_size = len(media_data) if media_data else 0
    msg_refs = {}
    _refs_lock = threading.Lock()

    def _send_pushover_all():
        po = config.get("pushover", {})
        if not po.get("enabled"):
            return
        cam_overrides = {}
        for co in po.get("camera_overrides", []):
            if co.get("camera") == camera:
                cam_overrides = co
                break
        po_refs = []
        for r in po.get("recipients", []):
            try:
                nsess = _get_notifier_session()
                def _do_send(r=r, nsess=nsess):
                    return send_pushover(
                        po, r, title, message, media_data, media_type,
                        url=frigate_url, video_url=video_url, snooze_url=snooze_url,
                        camera_overrides=cam_overrides, session=nsess,
                    )
                status = _retry_provider(_do_send, f"Pushover/{r.get('name', '?')}")
                log_notification(review_id, event_id, camera, label_str, zone_str, "pushover", r.get("name", ""), status, gif_size=media_size)
                # Only save ref if Phase 1 succeeded (not on error)
                if not _is_error_result(status):
                    po_refs.append({"config": po, "recipient": r, "camera_overrides": cam_overrides})
            except Exception as e:
                log.error("Pushover send error (%s): %s", r.get("name", "?"), e)
                _inc_stat("errors")
        with _refs_lock:
            msg_refs["pushover"] = po_refs

    def _send_discord_all():
        dc = config.get("discord", {})
        if not dc.get("enabled"):
            return
        dc_refs = []
        for w in dc.get("webhooks", []):
            try:
                nsess = _get_notifier_session()
                def _do_send(w=w, nsess=nsess):
                    return send_discord(
                        w, title, message, media_data, media_type,
                        url=frigate_url, video_url=video_url,
                        camera=camera, label=label_str, zone=zone_str,
                        session=nsess,
                    )
                result = _retry_provider(_do_send, f"Discord/{w.get('name', '?')}")
                if isinstance(result, tuple):
                    status, message_id = result
                else:
                    status, message_id = result, None
                log_notification(review_id, event_id, camera, label_str, zone_str, "discord", w.get("name", ""), status, gif_size=media_size)
                # Only save ref if send succeeded
                if message_id and not _is_error_result(status):
                    dc_refs.append({"webhook_url": w.get("url", ""), "message_id": message_id})
            except Exception as e:
                log.error("Discord send error (%s): %s", w.get("name", "?"), e)
                _inc_stat("errors")
        with _refs_lock:
            msg_refs["discord"] = dc_refs

    def _send_telegram():
        tg = config.get("telegram", {})
        if not tg.get("enabled"):
            return
        try:
            def _do_send():
                return send_telegram(tg, title, message, media_data, media_type, url=frigate_url, video_url=video_url)
            status = _retry_provider(_do_send, "Telegram")
            log_notification(review_id, event_id, camera, label_str, zone_str, "telegram", tg.get("chat_id", ""), status, gif_size=media_size)
        except Exception as e:
            log.error("Telegram send error: %s", e)
            _inc_stat("errors")

    def _send_ntfy():
        nt = config.get("ntfy", {})
        if not nt.get("enabled"):
            return
        try:
            def _do_send():
                return send_ntfy(nt, title, message, media_data, media_type, url=frigate_url)
            status = _retry_provider(_do_send, "Ntfy")
            log_notification(review_id, event_id, camera, label_str, zone_str, "ntfy", nt.get("topic", ""), status, gif_size=media_size)
        except Exception as e:
            log.error("Ntfy send error: %s", e)
            _inc_stat("errors")

    def _send_gotify():
        go = config.get("gotify", {})
        if not go.get("enabled"):
            return
        try:
            def _do_send():
                return send_gotify(go, title, message, media_data, media_type, url=frigate_url)
            status = _retry_provider(_do_send, "Gotify")
            log_notification(review_id, event_id, camera, label_str, zone_str, "gotify", "server", status, gif_size=media_size)
        except Exception as e:
            log.error("Gotify send error: %s", e)
            _inc_stat("errors")

    def _send_smtp():
        em = config.get("smtp", {})
        if not em.get("enabled"):
            return
        try:
            def _do_send():
                return send_smtp(em, title, message, media_data, media_type, url=frigate_url)
            status = _retry_provider(_do_send, "SMTP")
            log_notification(review_id, event_id, camera, label_str, zone_str, "email", ", ".join(em.get("recipients", [])), status, gif_size=media_size)
        except Exception as e:
            log.error("SMTP send error: %s", e)
            _inc_stat("errors")

    def _send_webhook():
        wh = config.get("webhook", {})
        if not wh.get("enabled"):
            return
        try:
            event_data = {"review_id": review_id, "event_id": event_id, "camera": camera, "label": label_str, "zones": zone_str, "video_url": video_url}
            def _do_send():
                return send_webhook(wh, title, message, media_data, media_type, url=frigate_url, event_data=event_data)
            status = _retry_provider(_do_send, "Webhook")
            log_notification(review_id, event_id, camera, label_str, zone_str, "webhook", wh.get("url", "")[:30], status, gif_size=media_size)
        except Exception as e:
            log.error("Webhook send error: %s", e)
            _inc_stat("errors")

    senders = [_send_pushover_all, _send_discord_all, _send_telegram, _send_ntfy, _send_gotify, _send_smtp, _send_webhook]
    send_futures = []
    for sender in senders:
        send_futures.append(provider_pool.submit(sender))

    try:
        for f in as_completed(send_futures, timeout=45):
            try:
                f.result()
            except Exception as e:
                log.error("Provider dispatch error: %s", e)
                _inc_stat("errors")
    except TimeoutError:
        log.error("Provider dispatch timeout (45s) - some providers may still be running")
        _inc_stat("errors")

    return msg_refs


def update_all_providers(msg_refs, title, message, media_data, media_type, media_ext,
                          frigate_url, video_url, review_id, event_id,
                          camera, label_str, zone_str):
    """Update previously sent notifications with GIF media (parallel via provider_pool)."""
    media_size = len(media_data) if media_data else 0
    snooze_url = build_snooze_url(30)
    futures = []

    def _update_discord():
        for ref in msg_refs.get("discord", []):
            try:
                nsess = _get_notifier_session()
                def _do_update(ref=ref, nsess=nsess):
                    return update_discord(
                        ref["webhook_url"], ref["message_id"],
                        title, message, media_data, media_type,
                        url=frigate_url, video_url=video_url,
                        camera=camera, label=label_str, zone=zone_str,
                        session=nsess,
                    )
                status = _retry_provider(_do_update, "Discord/update")
                log_notification(review_id, event_id, camera, label_str, zone_str, "discord", "update", status, gif_size=media_size)
            except Exception as e:
                log.error("Discord update error: %s", e)

    def _update_pushover():
        for ref in msg_refs.get("pushover", []):
            try:
                nsess = _get_notifier_session()
                def _do_update(ref=ref, nsess=nsess):
                    return send_pushover(
                        ref["config"], ref["recipient"], title, message, media_data, media_type,
                        url=frigate_url, video_url=video_url, snooze_url=snooze_url,
                        camera_overrides=ref.get("camera_overrides", {}),
                        silent=True, session=nsess,
                    )
                status = _retry_provider(_do_update, f"Pushover/update/{ref['recipient'].get('name', '?')}")
                log_notification(review_id, event_id, camera, label_str, zone_str, "pushover", ref["recipient"].get("name", "") + " (gif)", status, gif_size=media_size)
            except Exception as e:
                log.error("Pushover update error: %s", e)

    futures.append(provider_pool.submit(_update_discord))
    futures.append(provider_pool.submit(_update_pushover))

    try:
        for f in as_completed(futures, timeout=45):
            try:
                f.result()
            except Exception as e:
                log.error("Provider update error: %s", e)
    except TimeoutError:
        log.error("Provider update timeout (45s) - some providers may still be running")


# ---------------------------------------------------------------------------
# Review processing - Phase 1 (instant snapshot) and Phase 2 (GIF upgrade)
# ---------------------------------------------------------------------------

def _add_to_dedup(review_id):
    """Thread-safe add to dedup set. Returns True if new, False if already seen."""
    with _dedup_lock:
        if review_id in notified_reviews:
            return False
        notified_reviews[review_id] = time.time()
        return True


def process_phase1(review):
    """Phase 1: Instant snapshot notification on event creation."""
    review_id = review.get("id", "")
    camera = review.get("camera", "")
    objects = review.get("data", {}).get("objects", [])
    detections = review.get("data", {}).get("detections", [])
    zones = review.get("data", {}).get("zones", [])

    _inc_stat("events_received")

    # Stale event filter (skip events older than 5 minutes)
    start_time = review.get("start_time", 0)
    if start_time and (time.time() - start_time) > 300:
        log.info("Skipping stale event %s (%.0fs old)", review_id, time.time() - start_time)
        _add_to_seen(review_id)
        _inc_stat("events_skipped")
        return

    if is_snoozed():
        log.info("Skipping %s: snoozed until %s", review_id, datetime.fromtimestamp(snooze_until).strftime("%H:%M"))
        _inc_stat("events_skipped")
        return
    if in_quiet_hours():
        log.info("Skipping %s: quiet hours active", review_id)
        _inc_stat("events_skipped")
        return
    if not should_notify(camera, objects, zones):
        _add_to_seen(review_id)
        _inc_stat("events_skipped")
        return
    if check_cooldown(camera):
        log.info("Skipping %s: camera %s in cooldown", review_id, camera)
        _inc_stat("events_skipped")
        return

    if not isinstance(detections, list) or not detections:
        _inc_stat("events_skipped")
        return

    if not _add_to_dedup(review_id):
        return

    matched_labels = filter_labels(objects)
    label_str = ", ".join(matched_labels) if matched_labels else ", ".join(objects)

    log.info("Phase 1: Review %s camera=%s objects=%s zones=%s", review_id, camera, matched_labels, zones)

    set_cooldown(camera)

    event_id = detections[0]
    zone_str = ", ".join(zones) if zones else ""
    title, message = build_message(camera, label_str, zone_str, review_id, event_id)
    frigate_url, video_url = build_event_urls(review_id, event_id)

    snap_data, snap_type, snap_ext = fetch_snapshot(event_id)
    if not snap_data:
        log.warning("No snapshot for %s, saving as pending", event_id)
        save_pending_event(review_id, review, "phase1")
        with _dedup_lock:
            notified_reviews.pop(review_id, None)
        return

    t_start = time.time()
    msg_refs = send_to_all_providers(
        title, message, snap_data, snap_type, snap_ext,
        frigate_url, video_url, review_id, event_id,
        camera, label_str, zone_str,
    )
    elapsed = time.time() - t_start
    log.info("Phase 1 SENT for %s in %.1fs (camera=%s)", review_id, elapsed, camera)
    _inc_stat("phase1_sent")

    remove_pending_event(review_id)

    # Store Phase 2 context for GIF upgrade when event ends
    media_type_cfg = config.get("media_type", "gif")
    if media_type_cfg != "snapshot":
        with _phase2_lock:
            if len(pending_phase2) >= 200:
                oldest_key = min(pending_phase2, key=lambda k: pending_phase2[k].get("created_at", 0))
                pending_phase2.pop(oldest_key, None)
                remove_phase2_context(oldest_key)
                log.warning("pending_phase2 full, evicted oldest entry %s", oldest_key)
            ctx = {
                "msg_refs": msg_refs,
                "title": title,
                "message": message,
                "frigate_url": frigate_url,
                "video_url": video_url,
                "event_id": event_id,
                "camera": camera,
                "label_str": label_str,
                "zone_str": zone_str,
                "created_at": time.time(),
            }
            pending_phase2[review_id] = ctx
        # Persist to SQLite so Phase 2 survives container restart
        save_phase2_context(review_id, ctx)


def process_phase2(review_id):
    """Phase 2: GIF upgrade when event ends.
    Uses atomic pop to prevent duplicate sends from concurrent triggers."""
    with _phase2_lock:
        ctx = pending_phase2.pop(review_id, None)
    if not ctx:
        return

    event_id = ctx["event_id"]
    log.info("Phase 2: Fetching GIF for %s (event %s)", review_id, event_id)

    # Small delay to let Frigate finalize the clip
    time.sleep(2)

    gif_data, gif_type, gif_ext = fetch_gif(event_id)
    if gif_data:
        t_start = time.time()
        update_all_providers(
            ctx["msg_refs"], ctx["title"], ctx["message"],
            gif_data, gif_type, gif_ext,
            ctx["frigate_url"], ctx["video_url"], review_id, event_id,
            ctx["camera"], ctx["label_str"], ctx["zone_str"],
        )
        elapsed = time.time() - t_start
        log.info("Phase 2 SENT for %s in %.1fs (%d bytes)", review_id, elapsed, len(gif_data))
        _inc_stat("phase2_sent")
        remove_phase2_context(review_id)
    else:
        # Put it back for timeout loop to retry once more
        with _phase2_lock:
            if review_id not in pending_phase2:
                ctx["_phase2_retried"] = True
                pending_phase2[review_id] = ctx
                log.warning("Phase 2: No GIF for %s, put back for timeout retry", event_id)
            else:
                log.info("Phase 2: No GIF for %s, already re-queued", event_id)


def phase2_timeout_loop():
    """Background loop: trigger Phase 2 for events that never got an 'end' message."""
    while not shutting_down.is_set():
        timeout = config.get("phase2_timeout", 60)
        now = time.time()
        expired = []
        with _phase2_lock:
            for rid, ctx in list(pending_phase2.items()):
                if now - ctx["created_at"] > timeout:
                    expired.append(rid)

        for rid in expired:
            if shutting_down.is_set():
                return
            try:
                with _phase2_lock:
                    ctx = pending_phase2.pop(rid, None)
                if not ctx:
                    continue

                already_retried = ctx.get("_phase2_retried", False)
                event_id = ctx["event_id"]

                if already_retried:
                    log.info("Phase 2 final timeout for %s, last GIF attempt", rid)
                else:
                    log.info("Phase 2 timeout for %s, attempting GIF", rid)

                time.sleep(1)
                gif_data, gif_type, gif_ext = fetch_gif(event_id, retries=2, retry_interval=3)
                if gif_data:
                    update_all_providers(
                        ctx["msg_refs"], ctx["title"], ctx["message"],
                        gif_data, gif_type, gif_ext,
                        ctx["frigate_url"], ctx["video_url"], rid, event_id,
                        ctx["camera"], ctx["label_str"], ctx["zone_str"],
                    )
                    log.info("Phase 2 timeout SENT for %s (%d bytes)", rid, len(gif_data))
                    _inc_stat("phase2_sent")
                else:
                    log.info("Phase 2 timeout: No GIF for %s, snapshot stands", event_id)
                remove_phase2_context(rid)
            except Exception as e:
                log.error("Phase 2 timeout error for %s: %s", rid, e)

        for _ in range(5):
            if shutting_down.is_set():
                return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Pending event retry loop
# ---------------------------------------------------------------------------

def pending_retry_loop():
    """Retry failed Phase 1 notifications from the persistent queue."""
    while not shutting_down.is_set():
        try:
            pending = get_pending_events()
            for row in pending:
                if shutting_down.is_set():
                    return
                review_id = row["review_id"]
                try:
                    event_data = json.loads(row["event_data"])
                    log.info("Retrying pending event %s (attempt %d)", review_id, row["attempts"] + 1)
                    with _dedup_lock:
                        notified_reviews.pop(review_id, None)
                        seen_reviews.pop(review_id, None)
                    process_phase1(event_data)
                except json.JSONDecodeError:
                    log.error("Pending event %s has corrupt JSON, removing", review_id)
                    remove_pending_event(review_id)
                except Exception as e:
                    log.error("Pending retry error for %s: %s", review_id, e)
                    increment_pending_retry(review_id, row["attempts"])
        except Exception as e:
            log.error("Pending retry loop error: %s", e)
        for _ in range(10):
            if shutting_down.is_set():
                return
            time.sleep(1)


# ---------------------------------------------------------------------------
# Mode 1: API Polling (fallback, catches anything MQTT misses)
# ---------------------------------------------------------------------------

def poll_frigate():
    global poller_running, _last_poll_error, _poll_error_count
    poller_running = True
    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    poll_interval = config.get("poll_interval", 5)
    lookback = 120
    session = _get_session()

    log.info("API poller started (interval=%ds, url=%s)", poll_interval, frigate_url)

    # Grace period on startup to let MQTT connect first
    for _ in range(3):
        if shutting_down.is_set():
            return
        time.sleep(1)

    while poller_running and not shutting_down.is_set():
        # Circuit breaker: skip poll if Frigate is known down
        if not _frigate_circuit_ok():
            for _ in range(poll_interval):
                if not poller_running or shutting_down.is_set():
                    return
                time.sleep(1)
            continue

        try:
            params = {"severity": "alert", "after": time.time() - lookback, "limit": 50}
            resp = session.get(f"{frigate_url}/api/review", params=params, timeout=10)
            if resp.status_code == 200:
                reviews = resp.json()
                _last_poll_error = ""
                _poll_error_count = 0
                _frigate_success()

                for review in reviews:
                    if review.get("severity") == "alert":
                        review_id = review.get("id", "")
                        with _dedup_lock:
                            already_handled = review_id in notified_reviews or review_id in seen_reviews
                        if not already_handled:
                            event_pool.submit(process_phase1, review)
                        if review.get("end_time"):
                            with _phase2_lock:
                                has_pending = review_id in pending_phase2
                            if has_pending:
                                event_pool.submit(process_phase2, review_id)
            else:
                _frigate_failure()
                _handle_poll_error(f"HTTP {resp.status_code}")
        except requests.exceptions.ConnectionError:
            _frigate_failure()
            _handle_poll_error("Frigate connection refused")
        except requests.exceptions.Timeout:
            _frigate_failure()
            _handle_poll_error("Frigate request timeout")
        except Exception as e:
            _frigate_failure()
            _handle_poll_error(str(e))

        for _ in range(poll_interval):
            if not poller_running or shutting_down.is_set():
                return
            time.sleep(1)


def _handle_poll_error(error_msg):
    """Suppress repeated identical poll errors to avoid log spam."""
    global _last_poll_error, _poll_error_count
    if error_msg == _last_poll_error:
        _poll_error_count += 1
        if _poll_error_count % 12 == 1:
            log.warning("API poll error (repeated %dx): %s", _poll_error_count, error_msg)
    else:
        _last_poll_error = error_msg
        _poll_error_count = 1
        log.warning("API poll error: %s", error_msg)


def start_poller():
    thread = threading.Thread(target=poll_frigate, daemon=True)
    thread.start()
    return thread


def stop_poller():
    global poller_running
    poller_running = False


# ---------------------------------------------------------------------------
# Mode 2: MQTT (primary, instant notifications) — QoS 1 for guaranteed delivery
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, rc, properties=None):
    global mqtt_connected
    if rc == 0:
        topic = f"{config.get('mqtt', {}).get('topic_prefix', 'frigate')}/reviews"
        client.subscribe(topic, qos=1)  # QoS 1: at-least-once delivery
        mqtt_connected = True
        log.info("MQTT connected, subscribed to %s (QoS 1)", topic)
    else:
        mqtt_connected = False
        log.error("MQTT connection failed, rc=%d", rc)


def on_disconnect(client, userdata, rc, *args):
    global mqtt_connected
    mqtt_connected = False
    if rc != 0:
        log.warning("MQTT disconnected unexpectedly (rc=%d), will reconnect", rc)


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload)
        msg_type = payload.get("type")
        review = payload.get("after", {})
        severity = review.get("severity", "")

        if severity != "alert":
            return

        if msg_type == "new":
            event_pool.submit(process_phase1, review)

        elif msg_type == "update":
            review_id = review.get("id", "")
            with _dedup_lock:
                already_sent = review_id in notified_reviews
            if not already_sent:
                camera = review.get("camera", "")
                objects = review.get("data", {}).get("objects", [])
                zones = review.get("data", {}).get("zones", [])
                if should_notify(camera, objects, zones):
                    with _dedup_lock:
                        seen_reviews.pop(review_id, None)
                    log.info("Update promoted to Phase 1: %s camera=%s objects=%s", review_id, camera, objects)
                    event_pool.submit(process_phase1, review)

        elif msg_type == "end":
            review_id = review.get("id", "")
            if review_id:
                event_pool.submit(process_phase2, review_id)

    except json.JSONDecodeError:
        log.warning("MQTT: received invalid JSON")
    except Exception as e:
        log.error("MQTT message processing error: %s", e)


def start_mqtt():
    global mqtt_client
    import paho.mqtt.client as mqtt

    mqtt_conf = config.get("mqtt", {})
    if not mqtt_conf.get("server"):
        log.warning("MQTT server not configured")
        return

    mqtt_client = mqtt.Client(
        client_id=mqtt_conf.get("client_id", "frigate-alerts"),
        callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    )

    if mqtt_conf.get("username"):
        mqtt_client.username_pw_set(mqtt_conf["username"], mqtt_conf.get("password", ""))

    mqtt_client.on_connect = on_connect
    mqtt_client.on_disconnect = on_disconnect
    mqtt_client.on_message = on_message

    mqtt_client.reconnect_delay_set(min_delay=1, max_delay=60)

    try:
        mqtt_client.connect_async(mqtt_conf["server"], mqtt_conf.get("port", 1883), keepalive=60)
        mqtt_client.loop_start()
        log.info("MQTT client started (server=%s:%d)", mqtt_conf["server"], mqtt_conf.get("port", 1883))
    except Exception as e:
        log.error("MQTT connection error: %s", e)


def stop_mqtt():
    global mqtt_client, mqtt_connected
    if mqtt_client:
        try:
            mqtt_client.loop_stop()
            mqtt_client.disconnect()
        except Exception:
            pass
        mqtt_client = None
        mqtt_connected = False


# ---------------------------------------------------------------------------
# Event video page HTML
# ---------------------------------------------------------------------------

EVENT_PAGE_HTML = """<!DOCTYPE html>
<html lang="en"><head><meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{label} - {camera} | Frigate Alerts</title>
<style>
*{{margin:0;padding:0;box-sizing:border-box}}
body{{background:#0a0a0a;color:#e4e4e7;font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;display:flex;flex-direction:column;align-items:center;min-height:100vh;padding:20px}}
h1{{font-size:1.2rem;margin-bottom:4px}}h1 span{{color:#3b82f6}}
.sub{{color:#71717a;font-size:.85rem;margin-bottom:16px}}
.player{{width:100%;max-width:800px;border-radius:8px;overflow:hidden;background:#141414;border:1px solid #2a2a2a}}
video,img{{width:100%;display:block}}
.links{{margin-top:12px;display:flex;gap:12px;flex-wrap:wrap}}
.links a{{color:#3b82f6;text-decoration:none;font-size:.88rem}}.links a:hover{{text-decoration:underline}}
.info{{margin-top:16px;background:#141414;border:1px solid #2a2a2a;border-radius:8px;padding:12px;font-size:.85rem;max-width:800px;width:100%}}
.info div{{display:flex;justify-content:space-between;padding:4px 0;border-bottom:1px solid #1e1e1e}}
.info div:last-child{{border:none}}
.info .k{{color:#71717a}}.info .v{{text-align:right}}
</style></head><body>
<h1>Frigate <span>Alerts</span></h1>
<p class="sub">{label} - {camera}</p>
<div class="player">{player}</div>
<div class="links">{links}</div>
<div class="info">{info}</div>
</body></html>"""


def build_event_page(event_id):
    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    frigate_public = config.get("frigate", {}).get("public_url", "")
    session = _get_session()
    try:
        resp = session.get(f"{frigate_url}/api/events/{event_id}", timeout=10)
        if resp.status_code != 200:
            return None
        event = resp.json()
    except Exception:
        return None

    camera = event.get("camera", "unknown")
    label = event.get("label", "unknown").title()
    start = datetime.fromtimestamp(event.get("start_time", 0)).strftime("%Y-%m-%d %H:%M:%S")
    zones = ", ".join(event.get("zones", [])) or "none"
    media_base = frigate_public or frigate_url
    clip_url = f"{media_base}/api/events/{event_id}/clip.mp4"
    gif_url = f"{media_base}/api/events/{event_id}/preview.gif"
    snap_url = f"{media_base}/api/events/{event_id}/snapshot.jpg"

    if event.get("has_clip"):
        player = f'<video controls autoplay loop muted playsinline poster="{snap_url}"><source src="{clip_url}" type="video/mp4"></video>'
    else:
        player = f'<img src="{gif_url}" alt="Preview" onerror="this.src=\'{snap_url}\'">'

    links_parts = []
    if frigate_public:
        links_parts.append(f'<a href="{frigate_public}/review?id={event.get("id", "")}">Open in Frigate</a>')
    links_parts.append(f'<a href="{clip_url}" download>Download Clip</a>')
    links_parts.append(f'<a href="{snap_url}" download>Download Snapshot</a>')

    info = f"""<div><span class="k">Camera</span><span class="v">{camera}</span></div>
<div><span class="k">Label</span><span class="v">{label}</span></div>
<div><span class="k">Zones</span><span class="v">{zones}</span></div>
<div><span class="k">Time</span><span class="v">{start}</span></div>
<div><span class="k">Event ID</span><span class="v" style="font-family:monospace;font-size:.78rem">{event_id}</span></div>"""

    return EVENT_PAGE_HTML.format(
        label=label, camera=camera,
        player=player, links=" ".join(links_parts), info=info,
    )


# ---------------------------------------------------------------------------
# FastAPI application
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    load_config()
    init_db()
    cleanup_history()

    # Restore Phase 2 contexts from SQLite (survives restarts)
    load_pending_phase2()

    # Start MQTT (primary)
    mqtt_conf = config.get("mqtt", {})
    if mqtt_conf.get("enabled") and mqtt_conf.get("server"):
        start_mqtt()
        log.info("MQTT mode enabled (primary, QoS 1)")
    else:
        log.info("MQTT not configured, using API polling only")

    # Start API poller (always runs as fallback/safety net)
    start_poller()

    # Start Phase 2 timeout checker
    threading.Thread(target=phase2_timeout_loop, daemon=True).start()

    # Start pending event retry loop
    threading.Thread(target=pending_retry_loop, daemon=True).start()

    # Start cleanup loop
    threading.Thread(target=run_cleanup_loop, daemon=True).start()

    log.info("Frigate Alerts v2.4 started (MQTT=%s, poll=%ds, event_pool=%d, provider_pool=%d)",
             "enabled" if mqtt_conf.get("enabled") and mqtt_conf.get("server") else "disabled",
             config.get("poll_interval", 5),
             event_pool._max_workers,
             provider_pool._max_workers)
    yield

    # Graceful shutdown
    shutting_down.set()
    stop_poller()
    stop_mqtt()
    event_pool.shutdown(wait=True, cancel_futures=True)
    provider_pool.shutdown(wait=True, cancel_futures=True)
    log.info("Frigate Alerts shut down")


app = FastAPI(title="Frigate Alerts", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text())


@app.get("/event/{event_id}", response_class=HTMLResponse)
async def event_page(event_id: str):
    html = build_event_page(event_id)
    if html:
        return HTMLResponse(html)
    return HTMLResponse("<h1>Event not found</h1>", status_code=404)


@app.get("/api/config")
async def get_config():
    return json.loads(json.dumps(config))


@app.post("/api/config")
async def update_config(request: Request):
    new_config = await request.json()
    save_config(new_config)
    stop_mqtt()
    mqtt_conf = new_config.get("mqtt", {})
    if mqtt_conf.get("enabled") and mqtt_conf.get("server"):
        start_mqtt()
    stop_poller()
    start_poller()
    return {"status": "ok"}


@app.get("/api/history")
async def history(limit: int = 50):
    return get_history(limit)


@app.get("/api/status")
async def status():
    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    frigate_ok = False
    session = _get_session()
    try:
        resp = session.get(f"{frigate_url}/api/stats", timeout=5)
        frigate_ok = resp.status_code == 200
    except Exception:
        pass

    mqtt_conf = config.get("mqtt", {})
    mqtt_enabled = bool(mqtt_conf.get("enabled") and mqtt_conf.get("server"))

    with _stats_lock:
        stats_copy = dict(stats)

    with _frigate_cb_lock:
        cb_failures = _frigate_consecutive_failures
        cb_open = _frigate_circuit_open_until > time.time()

    return {
        "mqtt_enabled": mqtt_enabled,
        "mqtt_connected": mqtt_connected if mqtt_enabled else None,
        "frigate_connected": frigate_ok,
        "frigate_circuit_breaker": "open" if cb_open else ("degraded" if cb_failures > 0 else "ok"),
        "poller_running": poller_running,
        "snoozed": is_snoozed(),
        "snooze_remaining": max(0, int(snooze_until - time.time())) if is_snoozed() else 0,
        "quiet_hours_active": in_quiet_hours(),
        "pending_phase2": len(pending_phase2),
        "stats": stats_copy,
    }


@app.post("/api/snooze")
async def snooze(request: Request):
    global snooze_until
    body = await request.json()
    minutes = body.get("minutes", 30)
    if minutes <= 0:
        snooze_until = 0
        return {"status": "ok", "snoozed": False}
    snooze_until = time.time() + (minutes * 60)
    log.info("Snoozed for %d minutes", minutes)
    return {"status": "ok", "snoozed": True, "minutes": minutes, "until": snooze_until}


@app.post("/api/snooze/cancel")
async def snooze_cancel():
    global snooze_until
    snooze_until = 0
    log.info("Snooze cancelled")
    return {"status": "ok", "snoozed": False}


@app.get("/api/snooze/quick")
async def snooze_quick(minutes: int = 30):
    global snooze_until
    snooze_until = time.time() + (minutes * 60)
    log.info("Snoozed for %d minutes (via quick snooze)", minutes)
    return HTMLResponse(f"""<!DOCTYPE html><html><head><meta charset="UTF-8">
<style>body{{background:#0a0a0a;color:#22c55e;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;text-align:center}}</style>
</head><body><div><h2>Snoozed for {minutes} minutes</h2><p style="color:#71717a;margin-top:8px">You can close this page.</p></div></body></html>""")


@app.post("/api/test")
async def test_notification():
    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    session = _get_session()
    try:
        resp = session.get(f"{frigate_url}/api/events?limit=1&has_clip=1", timeout=10)
        events = resp.json()
        if not events:
            return {"status": "error", "message": "No events in Frigate"}
        event = events[0]
    except Exception as e:
        return {"status": "error", "message": str(e)}

    event_id = event["id"]
    title = f"Test - {event['label'].title()} on {event['camera']}"
    message = "Test notification from Frigate Alerts"
    frigate_review_url, video_url = build_event_urls("test", event_id)

    snap_data, snap_type, snap_ext = fetch_snapshot(event_id)
    if not snap_data:
        return {"status": "error", "message": "Could not fetch snapshot"}

    def _run_test():
        msg_refs = send_to_all_providers(
            title, message, snap_data, snap_type, snap_ext,
            frigate_review_url, video_url, "test", event_id,
            event["camera"], event["label"], "",
        )
        log.info("Test Phase 1: Snapshot sent")

        gif_data, gif_type, gif_ext = fetch_gif(event_id, retries=2, retry_interval=3)
        if gif_data:
            update_all_providers(
                msg_refs, title, message, gif_data, gif_type, gif_ext,
                frigate_review_url, video_url, "test", event_id,
                event["camera"], event["label"], "",
            )
            log.info("Test Phase 2: GIF upgrade sent")
        else:
            log.info("Test Phase 2: No GIF available, snapshot stands")

    event_pool.submit(_run_test)
    return {"status": "ok", "camera": event["camera"], "label": event["label"]}


@app.get("/api/health")
async def health():
    """Health check — verifies core subsystems are alive."""
    healthy = True
    details = {}

    try:
        f = event_pool.submit(lambda: True)
        f.result(timeout=2)
        details["event_pool"] = "ok"
    except Exception:
        details["event_pool"] = "unhealthy"
        healthy = False

    try:
        f = provider_pool.submit(lambda: True)
        f.result(timeout=2)
        details["provider_pool"] = "ok"
    except Exception:
        details["provider_pool"] = "unhealthy"
        healthy = False

    mqtt_conf = config.get("mqtt", {})
    if mqtt_conf.get("enabled") and mqtt_conf.get("server"):
        details["mqtt"] = "connected" if mqtt_connected else "disconnected"

    details["poller"] = "running" if poller_running else "stopped"

    status_code = 200 if healthy else 503
    return JSONResponse({"status": "ok" if healthy else "unhealthy", **details}, status_code=status_code)


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
