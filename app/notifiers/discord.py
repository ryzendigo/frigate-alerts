"""Discord notification provider. Rich embeds with GIF/video attachments and message editing."""

import json
import logging
import requests
from datetime import datetime, timezone

log = logging.getLogger("frigate-alerts")

# Embed colors
COLOR_ALERT = 0xEF4444    # Red — Phase 1 (new detection)
COLOR_UPDATED = 0x22C55E  # Green — Phase 2 (GIF upgrade, event resolved)

# Branded webhook identity
WEBHOOK_USERNAME = "Frigate Alerts"
WEBHOOK_AVATAR = "https://raw.githubusercontent.com/blakeblackshear/frigate/dev/docs/static/img/frigate.png"


def _build_embed(title, message, camera, label, zone, url, video_url, ext, is_update=False):
    """Build a Discord rich embed dict."""
    embed = {
        "title": title,
        "description": message,
        "color": COLOR_UPDATED if is_update else COLOR_ALERT,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "fields": [],
        "footer": {
            "text": "Frigate Alerts" + (" • Updated with clip" if is_update else ""),
        },
    }

    if camera:
        embed["fields"].append({"name": "Camera", "value": camera, "inline": True})
    if label:
        embed["fields"].append({"name": "Label", "value": label.title(), "inline": True})
    if zone:
        embed["fields"].append({"name": "Zone", "value": zone, "inline": True})

    links = []
    if url:
        links.append(f"[View in Frigate]({url})")
    if video_url:
        links.append(f"[Watch Video]({video_url})")
    if links:
        embed["description"] += "\n\n" + " | ".join(links)

    if ext in ("gif", "jpg"):
        embed["image"] = {"url": f"attachment://preview.{ext}"}

    return embed


def _get_ext(image_type):
    """Determine file extension from MIME type."""
    return "gif" if "gif" in (image_type or "") else ("mp4" if "video" in (image_type or "") else "jpg")


def _handle_rate_limit(resp, retry_func, name, session=None):
    """Handle Discord 429 rate limiting with a single retry."""
    if resp.status_code == 429:
        try:
            retry_after = resp.json().get("retry_after", 1)
        except Exception:
            retry_after = 1
        log.warning("Discord rate limited for %s, retry_after=%.1fs", name, retry_after)
        import time
        time.sleep(min(retry_after, 5))
        return retry_func()
    return resp


def send_discord(webhook_config, title, message, image_data, image_type="image/gif",
                  url=None, video_url=None, camera="", label="", zone="",
                  session=None):
    webhook_url = webhook_config.get("url", "")
    if not webhook_url:
        return "error: no webhook URL", None

    ext = _get_ext(image_type)
    embed = _build_embed(title, message, camera, label, zone, url, video_url, ext, is_update=False)
    payload = {
        "username": WEBHOOK_USERNAME,
        "avatar_url": WEBHOOK_AVATAR,
        "embeds": [embed],
    }

    files = {
        "payload_json": (None, json.dumps(payload), "application/json"),
    }
    if image_data:
        files["file"] = (f"preview.{ext}", image_data, image_type)

    _post = session.post if session else requests.post
    name = webhook_config.get("name", "webhook")

    # Use ?wait=true to get the message object back (includes message ID for later editing)
    resp = _post(f"{webhook_url}?wait=true", files=files, timeout=15)

    # Handle Discord rate limiting
    def _retry():
        return _post(f"{webhook_url}?wait=true", files=files, timeout=15)
    resp = _handle_rate_limit(resp, _retry, name, session)

    if resp.status_code in (200, 204):
        message_id = None
        try:
            message_id = resp.json().get("id")
        except Exception:
            pass
        log.info("Discord sent to %s (msg_id=%s)", name, message_id)
        return "sent", message_id
    else:
        log.error("Discord error for %s: %s %s", name, resp.status_code, resp.text[:200])
        return f"error: {resp.status_code}", None


def update_discord(webhook_url, message_id, title, message, image_data, image_type="image/gif",
                    url=None, video_url=None, camera="", label="", zone="",
                    session=None):
    """Edit an existing Discord webhook message to replace the image with a GIF."""
    if not webhook_url or not message_id:
        return "error: missing webhook URL or message ID"

    ext = _get_ext(image_type)
    embed = _build_embed(title, message, camera, label, zone, url, video_url, ext, is_update=True)
    payload = {
        "username": WEBHOOK_USERNAME,
        "avatar_url": WEBHOOK_AVATAR,
        "embeds": [embed],
        "attachments": [],
    }

    files = {
        "payload_json": (None, json.dumps(payload), "application/json"),
    }
    if image_data:
        files["file"] = (f"preview.{ext}", image_data, image_type)

    _patch = session.patch if session else requests.patch
    patch_url = f"{webhook_url}/messages/{message_id}"
    resp = _patch(patch_url, files=files, timeout=15)

    # Handle Discord rate limiting
    def _retry():
        return _patch(patch_url, files=files, timeout=15)
    resp = _handle_rate_limit(resp, _retry, "edit", session)

    if resp.status_code in (200, 204):
        log.info("Discord message %s updated with GIF", message_id)
        return "updated"
    else:
        log.error("Discord edit error: %s %s", resp.status_code, resp.text[:200])
        return f"error: {resp.status_code}"
