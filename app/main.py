"""
Frigate Alerts - Main Application
Animated GIF notifications from Frigate NVR events.
Supports: Pushover, Discord, Telegram, Ntfy, Gotify, Email, Webhook

Two modes for receiving events (can run simultaneously):
  1. API Polling (default) - polls Frigate's /api/reviews endpoint.
     Only requires Frigate URL. No MQTT broker needed.
  2. MQTT (optional) - subscribes to frigate/reviews topic for
     instant notifications. Requires an MQTT broker.
"""

import json
import logging
import os
import sqlite3
import threading
import time
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
from .notifiers.discord import send_discord
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
cleanup_running = False
notified_reviews = set()

# Cooldown: camera -> last notification timestamp
camera_cooldowns = {}

# Snooze: timestamp when snooze expires (0 = not snoozed)
snooze_until = 0


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

def init_db():
    conn = sqlite3.connect(DB_PATH)
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
    conn.commit()
    conn.close()


def log_notification(review_id, event_id, camera, label, zones, provider, recipient, status, message="", gif_size=0):
    try:
        conn = sqlite3.connect(DB_PATH)
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
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM history ORDER BY timestamp DESC LIMIT ?", (limit,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception:
        return []


def cleanup_history():
    retention_days = config.get("history_retention_days", 30)
    cutoff = time.time() - (retention_days * 86400)
    try:
        conn = sqlite3.connect(DB_PATH)
        result = conn.execute("DELETE FROM history WHERE timestamp < ?", (cutoff,))
        deleted = result.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            log.info("Cleaned up %d history entries older than %d days", deleted, retention_days)
    except Exception as e:
        log.error("History cleanup error: %s", e)


def run_cleanup_loop():
    global cleanup_running
    cleanup_running = True
    while cleanup_running:
        cleanup_history()
        for _ in range(6 * 3600):
            if not cleanup_running:
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
    """Render a message template with {variable} substitution."""
    try:
        return template.format(**variables)
    except (KeyError, ValueError):
        return template


def build_message(camera, label_str, zone_str, review_id, event_id):
    """Build title and message from config templates or defaults."""
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
# Media fetching (GIF, snapshot, or clip)
# ---------------------------------------------------------------------------

def fetch_media(event_id, retries=None, retry_interval=None):
    """Fetch preferred media type for an event. Returns (data, mime_type, extension)."""
    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    retries = retries or config.get("gif_retries", 3)
    retry_interval = retry_interval or config.get("gif_retry_interval", 5)
    media_type = config.get("media_type", "gif")

    if media_type == "clip":
        # Try video clip first
        try:
            resp = requests.get(f"{frigate_url}/api/events/{event_id}/clip.mp4", timeout=60)
            if resp.status_code == 200 and len(resp.content) > 1000:
                log.info("Fetched clip for %s (%d bytes)", event_id, len(resp.content))
                return resp.content, "video/mp4", "mp4"
        except Exception as e:
            log.warning("Clip fetch error: %s", e)
        # Fall through to GIF

    if media_type in ("gif", "clip"):
        url = f"{frigate_url}/api/events/{event_id}/preview.gif"
        for attempt in range(1, retries + 1):
            try:
                resp = requests.get(url, timeout=30)
                if resp.status_code == 200 and len(resp.content) > 100:
                    log.info("Fetched GIF for %s (%d bytes)", event_id, len(resp.content))
                    return resp.content, "image/gif", "gif"
                log.warning("GIF not ready for %s (attempt %d/%d)", event_id, attempt, retries)
            except Exception as e:
                log.warning("GIF fetch error: %s", e)
            if attempt < retries:
                time.sleep(retry_interval)

    # Snapshot (either preferred or fallback)
    try:
        resp = requests.get(f"{frigate_url}/api/events/{event_id}/snapshot.jpg", timeout=30)
        if resp.status_code == 200:
            log.info("Fetched snapshot for %s", event_id)
            return resp.content, "image/jpeg", "jpg"
    except Exception as e:
        log.error("Snapshot fetch failed: %s", e)
    return None, None, None


# ---------------------------------------------------------------------------
# URL builders
# ---------------------------------------------------------------------------

def build_event_urls(review_id, event_id):
    """Build Frigate review URL and event video page URL."""
    frigate_public = config.get("frigate", {}).get("public_url", "")
    alerts_public = config.get("alerts_public_url", "")

    frigate_url = f"{frigate_public}/review?id={review_id}" if frigate_public else ""
    video_url = f"{alerts_public}/event/{event_id}" if alerts_public else ""

    return frigate_url, video_url


def build_snooze_url(minutes):
    """Build snooze API URL for Pushover action buttons."""
    alerts_public = config.get("alerts_public_url", "")
    if alerts_public:
        return f"{alerts_public}/api/snooze/quick?minutes={minutes}"
    return ""


# ---------------------------------------------------------------------------
# Notification dispatch
# ---------------------------------------------------------------------------

def send_to_all_providers(title, message, media_data, media_type, media_ext,
                          frigate_url, video_url, review_id, event_id,
                          camera, label_str, zone_str):
    snooze_url = build_snooze_url(30)
    media_size = len(media_data) if media_data else 0

    po = config.get("pushover", {})
    if po.get("enabled"):
        # Per-camera overrides
        cam_overrides = {}
        for co in po.get("camera_overrides", []):
            if co.get("camera") == camera:
                cam_overrides = co
                break

        for r in po.get("recipients", []):
            status = send_pushover(
                po, r, title, message, media_data, media_type,
                url=frigate_url, video_url=video_url, snooze_url=snooze_url,
                camera_overrides=cam_overrides,
            )
            log_notification(review_id, event_id, camera, label_str, zone_str, "pushover", r.get("name", ""), status, gif_size=media_size)

    dc = config.get("discord", {})
    if dc.get("enabled"):
        for w in dc.get("webhooks", []):
            status = send_discord(
                w, title, message, media_data, media_type,
                url=frigate_url, video_url=video_url,
                camera=camera, label=label_str, zone=zone_str,
            )
            log_notification(review_id, event_id, camera, label_str, zone_str, "discord", w.get("name", ""), status, gif_size=media_size)

    tg = config.get("telegram", {})
    if tg.get("enabled"):
        status = send_telegram(tg, title, message, media_data, media_type, url=frigate_url, video_url=video_url)
        log_notification(review_id, event_id, camera, label_str, zone_str, "telegram", tg.get("chat_id", ""), status, gif_size=media_size)

    nt = config.get("ntfy", {})
    if nt.get("enabled"):
        status = send_ntfy(nt, title, message, media_data, media_type, url=frigate_url)
        log_notification(review_id, event_id, camera, label_str, zone_str, "ntfy", nt.get("topic", ""), status, gif_size=media_size)

    go = config.get("gotify", {})
    if go.get("enabled"):
        status = send_gotify(go, title, message, media_data, media_type, url=frigate_url)
        log_notification(review_id, event_id, camera, label_str, zone_str, "gotify", "server", status, gif_size=media_size)

    em = config.get("smtp", {})
    if em.get("enabled"):
        status = send_smtp(em, title, message, media_data, media_type, url=frigate_url)
        log_notification(review_id, event_id, camera, label_str, zone_str, "email", ", ".join(em.get("recipients", [])), status, gif_size=media_size)

    wh = config.get("webhook", {})
    if wh.get("enabled"):
        event_data = {"review_id": review_id, "event_id": event_id, "camera": camera, "label": label_str, "zones": zone_str, "video_url": video_url}
        status = send_webhook(wh, title, message, media_data, media_type, url=frigate_url, event_data=event_data)
        log_notification(review_id, event_id, camera, label_str, zone_str, "webhook", wh.get("url", "")[:30], status, gif_size=media_size)


# ---------------------------------------------------------------------------
# Review processing (shared by poller and MQTT)
# ---------------------------------------------------------------------------

def process_review(review):
    global notified_reviews
    review_id = review.get("id", "")
    camera = review.get("camera", "")
    objects = review.get("data", {}).get("objects", [])
    detections = review.get("data", {}).get("detections", [])
    zones = review.get("data", {}).get("zones", [])

    if review_id in notified_reviews:
        return
    notified_reviews.add(review_id)
    if len(notified_reviews) > 1000:
        notified_reviews = set(list(notified_reviews)[-500:])

    log.info("Review %s: camera=%s objects=%s zones=%s", review_id, camera, objects, zones)

    if is_snoozed():
        log.info("Skipping: snoozed until %s", datetime.fromtimestamp(snooze_until).strftime("%H:%M"))
        return
    if in_quiet_hours():
        log.info("Skipping: quiet hours active")
        return
    if not should_notify(camera, objects, zones):
        return
    if check_cooldown(camera):
        log.info("Skipping: camera %s in cooldown", camera)
        return
    if not detections:
        return

    set_cooldown(camera)

    event_id = detections[0]
    gif_delay = config.get("gif_delay", 10)
    log.info("Waiting %ds for media...", gif_delay)
    time.sleep(gif_delay)

    media_data, media_type, media_ext = fetch_media(event_id)
    if not media_data:
        return

    label_str = ", ".join(objects)
    zone_str = ", ".join(zones) if zones else ""
    title, message = build_message(camera, label_str, zone_str, review_id, event_id)
    frigate_url, video_url = build_event_urls(review_id, event_id)

    send_to_all_providers(
        title, message, media_data, media_type, media_ext,
        frigate_url, video_url, review_id, event_id,
        camera, label_str, zone_str,
    )


# ---------------------------------------------------------------------------
# Mode 1: API Polling (default, no MQTT needed)
# ---------------------------------------------------------------------------

def poll_frigate():
    global poller_running
    poller_running = True
    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    poll_interval = config.get("poll_interval", 10)
    last_check = time.time()

    log.info("API poller started (interval=%ds, url=%s)", poll_interval, frigate_url)

    while poller_running:
        try:
            params = {"severity": "alert", "after": last_check - 5, "limit": 20}
            resp = requests.get(f"{frigate_url}/api/reviews", params=params, timeout=15)
            if resp.status_code == 200:
                reviews = resp.json()
                now = time.time()
                for review in reviews:
                    if review.get("end_time") and review.get("severity") == "alert":
                        threading.Thread(target=process_review, args=(review,), daemon=True).start()
                last_check = now
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
# Mode 2: MQTT (optional, for instant notifications)
# ---------------------------------------------------------------------------

def on_connect(client, userdata, flags, rc, properties=None):
    global mqtt_connected
    if rc == 0:
        topic = f"{config.get('mqtt', {}).get('topic_prefix', 'frigate')}/reviews"
        client.subscribe(topic)
        mqtt_connected = True
        log.info("MQTT connected, subscribed to %s", topic)
    else:
        mqtt_connected = False


def on_disconnect(client, userdata, rc, *args):
    global mqtt_connected
    mqtt_connected = False


def on_message(client, userdata, msg):
    try:
        payload = json.loads(msg.payload)
        msg_type = payload.get("type")
        review = payload.get("after", {})
        severity = review.get("severity", "")
        if severity != "alert" or msg_type != "end":
            return
        threading.Thread(target=process_review, args=(review,), daemon=True).start()
    except json.JSONDecodeError:
        pass


def start_mqtt():
    global mqtt_client
    import paho.mqtt.client as mqtt
    mqtt_conf = config.get("mqtt", {})
    if not mqtt_conf.get("server"):
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
    mqtt_client.connect_async(mqtt_conf["server"], mqtt_conf.get("port", 1883), keepalive=60)
    mqtt_client.loop_start()


def stop_mqtt():
    global mqtt_client, mqtt_connected
    if mqtt_client:
        mqtt_client.loop_stop()
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
    """Build an HTML page for viewing an event's media."""
    frigate_url = config.get("frigate", {}).get("url", "http://frigate:5000")
    frigate_public = config.get("frigate", {}).get("public_url", "")

    # Get event details from Frigate
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

    # Use public Frigate URL for media if available, otherwise internal
    media_base = frigate_public or frigate_url

    # Build player (video > gif > snapshot)
    clip_url = f"{media_base}/api/events/{event_id}/clip.mp4"
    gif_url = f"{media_base}/api/events/{event_id}/preview.gif"
    snap_url = f"{media_base}/api/events/{event_id}/snapshot.jpg"

    if event.get("has_clip"):
        player = f'<video controls autoplay loop muted playsinline poster="{snap_url}"><source src="{clip_url}" type="video/mp4"></video>'
    else:
        player = f'<img src="{gif_url}" alt="Preview" onerror="this.src=\'{snap_url}\'">'

    # Links
    links_parts = []
    if frigate_public:
        links_parts.append(f'<a href="{frigate_public}/review?id={event.get("id", "")}">Open in Frigate</a>')
    links_parts.append(f'<a href="{clip_url}" download>Download Clip</a>')
    links_parts.append(f'<a href="{snap_url}" download>Download Snapshot</a>')

    # Info
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

    mqtt_conf = config.get("mqtt", {})
    if mqtt_conf.get("enabled") and mqtt_conf.get("server"):
        start_mqtt()
        log.info("MQTT mode enabled")
    else:
        log.info("MQTT not configured, using API polling only")

    start_poller()

    cleanup_thread = threading.Thread(target=run_cleanup_loop, daemon=True)
    cleanup_thread.start()

    log.info("Frigate Alerts started")
    yield

    global cleanup_running
    cleanup_running = False
    stop_poller()
    stop_mqtt()


app = FastAPI(title="Frigate Alerts", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTMLResponse((Path(__file__).parent / "static" / "index.html").read_text())


@app.get("/event/{event_id}", response_class=HTMLResponse)
async def event_page(event_id: str):
    """Event video/media page, linked from notifications."""
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
    """GET-based snooze for Pushover action buttons (they can only open URLs)."""
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

    media_data, media_type, media_ext = fetch_media(event["id"], retries=2, retry_interval=3)
    if not media_data:
        return {"status": "error", "message": "Could not fetch media"}

    title = f"Test - {event['label'].title()} on {event['camera']}"
    message = "Test notification from Frigate Alerts"
    frigate_review_url, video_url = build_event_urls("test", event["id"])
    send_to_all_providers(
        title, message, media_data, media_type, media_ext,
        frigate_review_url, video_url, "test", event["id"],
        event["camera"], event["label"], "",
    )
    return {"status": "ok", "camera": event["camera"], "label": event["label"]}


@app.get("/api/health")
async def health():
    return {"status": "ok", "uptime": time.time()}


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000)
