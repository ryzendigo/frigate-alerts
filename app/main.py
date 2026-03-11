"""
Frigate Alerts - Main Application (v2 - Rewritten for speed & reliability)
Animated GIF notifications from Frigate NVR events.
Supports: Pushover, Discord, Telegram, Ntfy, Gotify, Email, Webhook

Architecture:
  - MQTT primary (instant): subscribes to frigate/reviews, fires on "new"
  - API polling fallback: catches anything MQTT misses
  - Two-phase notifications:
      Phase 1: Instant snapshot on event creation (<2s target)
      Phase 2: GIF upgrade when event ends (or after timeout)
  - Event queue with SQLite persistence for guaranteed delivery
  - Parallel provider dispatch with ThreadPoolExecutor
  - Auto-reconnecting MQTT with exponential backoff
"""

import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

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
mqtt_reconnect_delay = 1
poller_running = False
cleanup_running = False
shutting_down = False

# Thread-safe dedup
_dedup_lock = threading.Lock()
notified_reviews = set()

# Phase 2 tracking: review_id -> {msg_refs, title, message, ...}
_phase2_lock = threading.Lock()
pending_phase2 = {}

# Cooldown: camera -> last notification timestamp
camera_cooldowns = {}

# Snooze: timestamp when snooze expires (0 = not snoozed)
snooze_until = 0

# Provider thread pool (reused across notifications)
provider_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="notify")

# Stats
stats = {"phase1_sent": 0, "phase2_sent": 0, "events_received": 0, "events_skipped": 0, "errors": 0}


def load_config():
    global config
    with open(CONFIG_PATH) as f:
        config = yaml.safe_load(f) or {}
    return config


def save_config(new_config):
    global config
    config = new_config
    with open(CONFIG_PATH, "w") as f:
        yaml.dump(config, f, default_flow_style=False, sort_keys=False)


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def init_db():
    conn = get_db()
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
    conn.commit()
    conn.close()


def log_notification(review_id, event_id, camera, label, zones, provider, recipient, status, message="", gif_size=0):
    try:
        conn = get_db()
        conn.execute(
            "INSERT INTO history (timestamp, review_id, event_id, camera, label, zones, provider, recipient, status, message, gif_size) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (time.time(), review_id, event_id, camera, label, zones, provider, recipient, status, message, gif_size),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("DB error: %s", e)


def get_history(limit=50):
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM history ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def save_pending_event(review_id, event_data, phase):
    try:
        conn = get_db()
        conn.execute(
            "INSERT OR REPLACE INTO pending_events (review_id, event_data, phase, attempts, next_retry, created_at) VALUES (?, ?, ?, 0, ?, ?)",
            (review_id, json.dumps(event_data), phase, time.time(), time.time()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Save pending event error: %s", e)


def remove_pending_event(review_id):
    try:
        conn = get_db()
        conn.execute("DELETE FROM pending_events WHERE review_id = ?", (review_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Remove pending event error: %s", e)


def get_pending_events():
    try:
        conn = get_db()
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM pending_events WHERE next_retry <= ? AND attempts < 3 ORDER BY created_at",
            (time.time(),),
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def increment_pending_retry(review_id):
    try:
        conn = get_db()
        conn.execute(
            "UPDATE pending_events SET attempts = attempts + 1, next_retry = ? WHERE review_id = ?",
            (time.time() + 30, review_id),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        log.error("Increment retry error: %s", e)


def cleanup_history():
    retention_days = config.get("history_retention_days", 30)
    cutoff = time.time() - (retention_days * 86400)
    try:
        conn = get_db()
        result = conn.execute("DELETE FROM history WHERE timestamp < ?", (cutoff,))
        deleted = result.rowcount
        # Also clean old pending events (>1 hour)
        conn.execute("DELETE FROM pending_events WHERE created_at < ?", (time.time() - 3600,))
        conn.commit()
        conn.close()
        if deleted > 0:
            log.info("Cleaned up %d history entries older than %d days", deleted, retention_days)
    except Exception as e:
        log.error("History cleanup error: %s", e)


def run_cleanup_loop():
    global cleanup_running
    cleanup_running = True
    while cleanup_running and not shutting_down:
        cleanup_history()
        for _ in range(6 * 3600):
            if not cleanup_running or shutting_down:
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
    last = camera_cooldowns.get(camera, 0)
    return time.time() - last < cooldown_secs


def set_cooldown(camera):
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


# ---------------------------------------------------------------------------
# Message template rendering
# ---------------------------------------------------------------------------

def render_template(template, variables):
    try:
        return template.format(**variables)
    except (KeyError, ValueError):
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
# Media fetching
# ---------------------------------------------------------------------------

def clip_to_gif(clip_data, max_duration=3, fps=6, width=240):
    with tempfile.NamedTemporaryFile(suffix=".mp4", delete=False) as mp4:
        mp4.write(clip_data)
        mp4_path = mp4.name
    gif_path = mp4_path.replace(".mp4", ".gif")
    try:
        cmd = [
            "ffmpeg", "-y", "-i", mp4_path,
            "-t", str(max_duration),
            "-vf", f"fps={fps},scale={width}:-1:flags=lanczos,split[s0][s1];[s0]palettegen=max_colors=128[p];[s1][p]paletteuse=dither=bayer",
            "-loop", "0",
            gif_path,
        ]
        result = subprocess.run(cmd, capture_output=True, timeout=30)
        if result.returncode == 0 and os.path.exists(gif_path):
            with open(gif_path, "rb") as f:
                gif_data = f.read()
            if len(gif_data) > 100:
                log.info("Generated GIF from clip (%d bytes)", len(gif_data))
                return gif_data
        else:
            log.warning("ffmpeg GIF conversion failed: %s", result.stderr[-200:] if result.stderr else "unknown")
    except Exception as e:
        log.warning("GIF conversion error: %s", e)
    finally:
        for p in (mp4_path, gif_path):
            try:
                os.unlink(p)
            except OSError:
                pass
    return None


def fetch_snapshot(event_id):
    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    for attempt in range(3):
        try:
            resp = requests.get(f"{frigate_url}/api/events/{event_id}/snapshot.jpg", timeout=5)
            if resp.status_code == 200 and len(resp.content) > 100:
                log.info("Fetched snapshot for %s (%d bytes)", event_id, len(resp.content))
                return resp.content, "image/jpeg", "jpg"
            if resp.status_code == 404 and attempt < 2:
                time.sleep(0.5)
                continue
        except Exception as e:
            log.error("Snapshot fetch failed (attempt %d): %s", attempt + 1, e)
            if attempt < 2:
                time.sleep(0.5)
    return None, None, None


def fetch_gif(event_id, retries=None, retry_interval=None):
    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    retries = retries or config.get("gif_retries", 3)
    retry_interval = retry_interval or config.get("gif_retry_interval", 5)

    for attempt in range(1, retries + 1):
        try:
            resp = requests.get(f"{frigate_url}/api/events/{event_id}/clip.mp4", timeout=30)
            if resp.status_code == 200 and len(resp.content) > 1000:
                gif_data = clip_to_gif(resp.content)
                if gif_data:
                    return gif_data, "image/gif", "gif"
            log.warning("Clip not ready for %s (attempt %d/%d)", event_id, attempt, retries)
        except Exception as e:
            log.warning("Clip fetch error: %s", e)
        if attempt < retries:
            time.sleep(retry_interval)

    # Fallback to Frigate's preview.gif
    try:
        resp = requests.get(f"{frigate_url}/api/events/{event_id}/preview.gif", timeout=15)
        if resp.status_code == 200 and len(resp.content) > 100:
            log.info("Fell back to Frigate preview.gif for %s (%d bytes)", event_id, len(resp.content))
            return resp.content, "image/gif", "gif"
    except Exception as e:
        log.warning("Preview GIF fallback error: %s", e)

    return None, None, None


def fetch_media(event_id, retries=None, retry_interval=None):
    media_type = config.get("media_type", "gif")
    if media_type == "clip":
        frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
        try:
            resp = requests.get(f"{frigate_url}/api/events/{event_id}/clip.mp4", timeout=30)
            if resp.status_code == 200 and len(resp.content) > 1000:
                return resp.content, "video/mp4", "mp4"
        except Exception as e:
            log.warning("Clip fetch error: %s", e)
    if media_type in ("gif", "clip"):
        data, mime, ext = fetch_gif(event_id, retries, retry_interval)
        if data:
            return data, mime, ext
    if media_type == "snapshot":
        return fetch_snapshot(event_id)
    return fetch_snapshot(event_id)


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
# Notification dispatch (parallel)
# ---------------------------------------------------------------------------

def send_to_all_providers(title, message, media_data, media_type, media_ext,
                          frigate_url, video_url, review_id, event_id,
                          camera, label_str, zone_str):
    """Send notification to all enabled providers IN PARALLEL. Returns message references."""
    snooze_url = build_snooze_url(30)
    media_size = len(media_data) if media_data else 0
    msg_refs = {}
    futures = []

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
                status = send_pushover(
                    po, r, title, message, media_data, media_type,
                    url=frigate_url, video_url=video_url, snooze_url=snooze_url,
                    camera_overrides=cam_overrides,
                )
                log_notification(review_id, event_id, camera, label_str, zone_str, "pushover", r.get("name", ""), status, gif_size=media_size)
            except Exception as e:
                log.error("Pushover send error: %s", e)
                stats["errors"] += 1
            po_refs.append({"config": po, "recipient": r, "camera_overrides": cam_overrides})
        msg_refs["pushover"] = po_refs

    def _send_discord_all():
        dc = config.get("discord", {})
        if not dc.get("enabled"):
            return
        dc_refs = []
        for w in dc.get("webhooks", []):
            try:
                status, message_id = send_discord(
                    w, title, message, media_data, media_type,
                    url=frigate_url, video_url=video_url,
                    camera=camera, label=label_str, zone=zone_str,
                )
                log_notification(review_id, event_id, camera, label_str, zone_str, "discord", w.get("name", ""), status, gif_size=media_size)
                if message_id:
                    dc_refs.append({"webhook_url": w.get("url", ""), "message_id": message_id})
            except Exception as e:
                log.error("Discord send error: %s", e)
                stats["errors"] += 1
        msg_refs["discord"] = dc_refs

    def _send_telegram():
        tg = config.get("telegram", {})
        if not tg.get("enabled"):
            return
        try:
            status = send_telegram(tg, title, message, media_data, media_type, url=frigate_url, video_url=video_url)
            log_notification(review_id, event_id, camera, label_str, zone_str, "telegram", tg.get("chat_id", ""), status, gif_size=media_size)
        except Exception as e:
            log.error("Telegram send error: %s", e)
            stats["errors"] += 1

    def _send_ntfy():
        nt = config.get("ntfy", {})
        if not nt.get("enabled"):
            return
        try:
            status = send_ntfy(nt, title, message, media_data, media_type, url=frigate_url)
            log_notification(review_id, event_id, camera, label_str, zone_str, "ntfy", nt.get("topic", ""), status, gif_size=media_size)
        except Exception as e:
            log.error("Ntfy send error: %s", e)
            stats["errors"] += 1

    def _send_gotify():
        go = config.get("gotify", {})
        if not go.get("enabled"):
            return
        try:
            status = send_gotify(go, title, message, media_data, media_type, url=frigate_url)
            log_notification(review_id, event_id, camera, label_str, zone_str, "gotify", "server", status, gif_size=media_size)
        except Exception as e:
            log.error("Gotify send error: %s", e)
            stats["errors"] += 1

    def _send_smtp():
        em = config.get("smtp", {})
        if not em.get("enabled"):
            return
        try:
            status = send_smtp(em, title, message, media_data, media_type, url=frigate_url)
            log_notification(review_id, event_id, camera, label_str, zone_str, "email", ", ".join(em.get("recipients", [])), status, gif_size=media_size)
        except Exception as e:
            log.error("SMTP send error: %s", e)
            stats["errors"] += 1

    def _send_webhook():
        wh = config.get("webhook", {})
        if not wh.get("enabled"):
            return
        try:
            event_data = {"review_id": review_id, "event_id": event_id, "camera": camera, "label": label_str, "zones": zone_str, "video_url": video_url}
            status = send_webhook(wh, title, message, media_data, media_type, url=frigate_url, event_data=event_data)
            log_notification(review_id, event_id, camera, label_str, zone_str, "webhook", wh.get("url", "")[:30], status, gif_size=media_size)
        except Exception as e:
            log.error("Webhook send error: %s", e)
            stats["errors"] += 1

    # Fire all providers in parallel
    senders = [_send_pushover_all, _send_discord_all, _send_telegram, _send_ntfy, _send_gotify, _send_smtp, _send_webhook]
    send_futures = []
    for sender in senders:
        send_futures.append(provider_pool.submit(sender))

    # Wait for all to complete (max 30s)
    for f in as_completed(send_futures, timeout=30):
        try:
            f.result()
        except Exception as e:
            log.error("Provider dispatch error: %s", e)
            stats["errors"] += 1

    return msg_refs


def update_all_providers(msg_refs, title, message, media_data, media_type, media_ext,
                          frigate_url, video_url, review_id, event_id,
                          camera, label_str, zone_str):
    """Update previously sent notifications with GIF media (parallel)."""
    media_size = len(media_data) if media_data else 0
    snooze_url = build_snooze_url(30)
    futures = []

    def _update_discord():
        for ref in msg_refs.get("discord", []):
            try:
                status = update_discord(
                    ref["webhook_url"], ref["message_id"],
                    title, message, media_data, media_type,
                    url=frigate_url, video_url=video_url,
                    camera=camera, label=label_str, zone=zone_str,
                )
                log_notification(review_id, event_id, camera, label_str, zone_str, "discord", "update", status, gif_size=media_size)
            except Exception as e:
                log.error("Discord update error: %s", e)

    def _update_pushover():
        for ref in msg_refs.get("pushover", []):
            try:
                status = send_pushover(
                    ref["config"], ref["recipient"], title, message, media_data, media_type,
                    url=frigate_url, video_url=video_url, snooze_url=snooze_url,
                    camera_overrides=ref.get("camera_overrides", {}),
                    silent=True,
                )
                log_notification(review_id, event_id, camera, label_str, zone_str, "pushover", ref["recipient"].get("name", "") + " (gif)", status, gif_size=media_size)
            except Exception as e:
                log.error("Pushover update error: %s", e)

    futures.append(provider_pool.submit(_update_discord))
    futures.append(provider_pool.submit(_update_pushover))

    for f in as_completed(futures, timeout=30):
        try:
            f.result()
        except Exception as e:
            log.error("Provider update error: %s", e)


# ---------------------------------------------------------------------------
# Review processing - Phase 1 (instant snapshot) and Phase 2 (GIF upgrade)
# ---------------------------------------------------------------------------

def _add_to_dedup(review_id):
    """Thread-safe add to dedup set. Returns True if new, False if already seen."""
    with _dedup_lock:
        if review_id in notified_reviews:
            return False
        notified_reviews.add(review_id)
        if len(notified_reviews) > 2000:
            # Keep most recent 1000
            sorted_ids = sorted(notified_reviews)
            notified_reviews.clear()
            notified_reviews.update(sorted_ids[-1000:])
        return True


def process_phase1(review):
    """Phase 1: Instant snapshot notification on event creation."""
    review_id = review.get("id", "")
    camera = review.get("camera", "")
    objects = review.get("data", {}).get("objects", [])
    detections = review.get("data", {}).get("detections", [])
    zones = review.get("data", {}).get("zones", [])

    # Thread-safe dedup
    if not _add_to_dedup(review_id):
        return

    stats["events_received"] += 1

    # Stale event filter (skip events older than 5 minutes)
    start_time = review.get("start_time", 0)
    if start_time and (time.time() - start_time) > 300:
        log.info("Skipping stale event %s (%.0fs old)", review_id, time.time() - start_time)
        stats["events_skipped"] += 1
        return

    log.info("Phase 1: Review %s camera=%s objects=%s zones=%s", review_id, camera, objects, zones)

    if is_snoozed():
        log.info("Skipping: snoozed until %s", datetime.fromtimestamp(snooze_until).strftime("%H:%M"))
        stats["events_skipped"] += 1
        return
    if in_quiet_hours():
        log.info("Skipping: quiet hours active")
        stats["events_skipped"] += 1
        return
    if not should_notify(camera, objects, zones):
        stats["events_skipped"] += 1
        return
    if check_cooldown(camera):
        log.info("Skipping: camera %s in cooldown", camera)
        stats["events_skipped"] += 1
        return
    if not detections:
        stats["events_skipped"] += 1
        return

    set_cooldown(camera)

    event_id = detections[0]
    label_str = ", ".join(objects)
    zone_str = ", ".join(zones) if zones else ""
    title, message = build_message(camera, label_str, zone_str, review_id, event_id)
    frigate_url, video_url = build_event_urls(review_id, event_id)

    # Fetch snapshot (available immediately on event creation)
    snap_data, snap_type, snap_ext = fetch_snapshot(event_id)
    if not snap_data:
        log.warning("No snapshot for %s, saving as pending", event_id)
        save_pending_event(review_id, review, "phase1")
        return

    t_start = time.time()
    msg_refs = send_to_all_providers(
        title, message, snap_data, snap_type, snap_ext,
        frigate_url, video_url, review_id, event_id,
        camera, label_str, zone_str,
    )
    elapsed = time.time() - t_start
    log.info("Phase 1 SENT for %s in %.1fs (camera=%s)", review_id, elapsed, camera)
    stats["phase1_sent"] += 1

    # Remove from pending if it was retried
    remove_pending_event(review_id)

    # Store Phase 2 context for GIF upgrade when event ends
    media_type_cfg = config.get("media_type", "gif")
    if media_type_cfg != "snapshot":
        with _phase2_lock:
            pending_phase2[review_id] = {
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


def process_phase2(review_id):
    """Phase 2: GIF upgrade when event ends."""
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
        stats["phase2_sent"] += 1
    else:
        log.info("Phase 2: No GIF available for %s, snapshot stands", event_id)


def phase2_timeout_loop():
    """Background loop: trigger Phase 2 for events that never got an 'end' message."""
    while not shutting_down:
        timeout = config.get("phase2_timeout", 60)
        now = time.time()
        expired = []
        with _phase2_lock:
            for rid, ctx in list(pending_phase2.items()):
                if now - ctx["created_at"] > timeout:
                    expired.append(rid)

        for rid in expired:
            log.info("Phase 2 timeout for %s, attempting GIF anyway", rid)
            try:
                process_phase2(rid)
            except Exception as e:
                log.error("Phase 2 timeout error for %s: %s", rid, e)

        time.sleep(5)


# ---------------------------------------------------------------------------
# Pending event retry loop
# ---------------------------------------------------------------------------

def pending_retry_loop():
    """Retry failed Phase 1 notifications from the persistent queue."""
    while not shutting_down:
        try:
            pending = get_pending_events()
            for row in pending:
                if shutting_down:
                    return
                review_id = row["review_id"]
                try:
                    event_data = json.loads(row["event_data"])
                    log.info("Retrying pending event %s (attempt %d)", review_id, row["attempts"] + 1)
                    # Remove from dedup so it can be reprocessed
                    with _dedup_lock:
                        notified_reviews.discard(review_id)
                    process_phase1(event_data)
                except Exception as e:
                    log.error("Pending retry error for %s: %s", review_id, e)
                    increment_pending_retry(review_id)
        except Exception as e:
            log.error("Pending retry loop error: %s", e)
        time.sleep(10)


# ---------------------------------------------------------------------------
# Mode 1: API Polling (fallback, catches anything MQTT misses)
# ---------------------------------------------------------------------------

def poll_frigate():
    global poller_running
    poller_running = True
    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    poll_interval = config.get("poll_interval", 5)
    lookback = 120  # Check last 2 minutes

    log.info("API poller started (interval=%ds, url=%s)", poll_interval, frigate_url)

    while poller_running and not shutting_down:
        try:
            params = {"severity": "alert", "after": time.time() - lookback, "limit": 50}
            resp = requests.get(f"{frigate_url}/api/review", params=params, timeout=10)
            if resp.status_code == 200:
                reviews = resp.json()
                for review in reviews:
                    # Process ANY alert review, not just ended ones
                    if review.get("severity") == "alert":
                        review_id = review.get("id", "")
                        # Quick check without lock for performance
                        if review_id not in notified_reviews:
                            threading.Thread(
                                target=process_phase1, args=(review,), daemon=True
                            ).start()
                        # Check for ended reviews to trigger Phase 2
                        if review.get("end_time") and review_id in pending_phase2:
                            threading.Thread(
                                target=process_phase2, args=(review_id,), daemon=True
                            ).start()
        except Exception as e:
            log.warning("API poll error: %s", e)
        time.sleep(poll_interval)


def start_poller():
    thread = threading.Thread(target=poll_frigate, daemon=True)
    thread.start()
    return thread


def stop_poller():
    global poller_running
    poller_running = False


# ---------------------------------------------------------------------------
# Mode 2: MQTT (primary, instant notifications)
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, rc, properties=None):
    global mqtt_connected, mqtt_reconnect_delay
    if rc == 0:
        topic = f"{config.get('mqtt', {}).get('topic_prefix', 'frigate')}/reviews"
        client.subscribe(topic)
        mqtt_connected = True
        mqtt_reconnect_delay = 1
        log.info("MQTT connected, subscribed to %s", topic)
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
            # Phase 1: Instant notification on event creation
            threading.Thread(target=process_phase1, args=(review,), daemon=True).start()

        elif msg_type == "end":
            # Phase 2: GIF upgrade when event ends
            review_id = review.get("id", "")
            if review_id:
                threading.Thread(target=process_phase2, args=(review_id,), daemon=True).start()

        # "update" type is ignored (intermediate updates during detection)

    except json.JSONDecodeError:
        pass
    except Exception as e:
        log.error("MQTT message processing error: %s", e)


def start_mqtt():
    global mqtt_client, mqtt_reconnect_delay
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

    # Enable auto-reconnect with backoff
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
    try:
        resp = requests.get(f"{frigate_url}/api/events/{event_id}", timeout=10)
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
    global shutting_down
    load_config()
    init_db()
    cleanup_history()

    # Start MQTT (primary)
    mqtt_conf = config.get("mqtt", {})
    if mqtt_conf.get("enabled") and mqtt_conf.get("server"):
        start_mqtt()
        log.info("MQTT mode enabled (primary)")
    else:
        log.info("MQTT not configured, using API polling only")

    # Start API poller (always runs as fallback/safety net)
    start_poller()

    # Start Phase 2 timeout checker
    p2_thread = threading.Thread(target=phase2_timeout_loop, daemon=True)
    p2_thread.start()

    # Start pending event retry loop
    retry_thread = threading.Thread(target=pending_retry_loop, daemon=True)
    retry_thread.start()

    # Start cleanup loop
    cleanup_thread = threading.Thread(target=run_cleanup_loop, daemon=True)
    cleanup_thread.start()

    log.info("Frigate Alerts v2 started (MQTT=%s, poll=%ds)",
             "enabled" if mqtt_connected or (mqtt_conf.get("enabled") and mqtt_conf.get("server")) else "disabled",
             config.get("poll_interval", 5))
    yield

    # Graceful shutdown
    shutting_down = True
    cleanup_running_flag = False
    stop_poller()
    stop_mqtt()
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
    try:
        resp = requests.get(f"{frigate_url}/api/stats", timeout=5)
        frigate_ok = resp.status_code == 200
    except Exception:
        pass

    mqtt_conf = config.get("mqtt", {})
    mqtt_enabled = bool(mqtt_conf.get("enabled") and mqtt_conf.get("server"))

    return {
        "mqtt_enabled": mqtt_enabled,
        "mqtt_connected": mqtt_connected if mqtt_enabled else None,
        "frigate_connected": frigate_ok,
        "poller_running": poller_running,
        "snoozed": is_snoozed(),
        "snooze_remaining": max(0, int(snooze_until - time.time())) if is_snoozed() else 0,
        "quiet_hours_active": in_quiet_hours(),
        "pending_phase2": len(pending_phase2),
        "stats": stats,
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
    try:
        resp = requests.get(f"{frigate_url}/api/events?limit=1&has_clip=1", timeout=10)
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

    threading.Thread(target=_run_test, daemon=True).start()
    return {"status": "ok", "camera": event["camera"], "label": event["label"]}


@app.get("/api/health")
async def health():
    return {"status": "ok", "uptime": time.time()}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
