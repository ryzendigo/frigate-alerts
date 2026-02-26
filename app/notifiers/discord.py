"""Discord notification provider. Rich embeds with GIF/video attachments and message editing."""

import json
import logging
import requests
from datetime import datetime

log = logging.getLogger("frigate-alerts")

# Embed color (blue accent matching the web UI)
EMBED_COLOR = 0x3B82F6


def send_discord(webhook_config, title, message, image_data, image_type="image/gif",
                  url=None, video_url=None, camera="", label="", zone=""):
    webhook_url = webhook_config.get("url", "")
    if not webhook_url:
        return "error: no webhook URL", None

    ext = "gif" if "gif" in (image_type or "") else ("mp4" if "video" in (image_type or "") else "jpg")

    # Build rich embed
    embed = {
        "title": title,
        "description": message,
        "color": EMBED_COLOR,
        "timestamp": datetime.utcnow().isoformat(),
        "fields": [],
    }

    if camera:
        embed["fields"].append({"name": "Camera", "value": camera, "inline": True})
    if label:
        embed["fields"].append({"name": "Label", "value": label.title(), "inline": True})
    if zone:
        embed["fields"].append({"name": "Zone", "value": zone, "inline": True})

    # Links
    links = []
    if url:
        links.append(f"[View in Frigate]({url})")
    if video_url:
        links.append(f"[Watch Video]({video_url})")
    if links:
        embed["description"] += "\n\n" + " | ".join(links)

    # For images, attach as embed image
    if ext in ("gif", "jpg"):
        embed["image"] = {"url": f"attachment://preview.{ext}"}

    payload = {"embeds": [embed]}

    files = {
        "payload_json": (None, json.dumps(payload), "application/json"),
    }
    if image_data:
        files["file"] = (f"preview.{ext}", image_data, image_type)

    try:
        # Use ?wait=true to get the message object back (includes message ID for later editing)
        resp = requests.post(f"{webhook_url}?wait=true", files=files, timeout=30)
        name = webhook_config.get("name", "webhook")
        if resp.status_code in (200, 204):
            message_id = None
            try:
                message_id = resp.json().get("id")
            except Exception:
                pass
            log.info("Discord sent to %s (msg_id=%s)", name, message_id)
            return "sent", message_id
        else:
            log.error("Discord error for %s: %s %s", name, resp.status_code, resp.text)
            return f"error: {resp.status_code}", None
    except Exception as e:
        log.error("Discord exception: %s", e)
        return f"error: {e}", None


def update_discord(webhook_url, message_id, title, message, image_data, image_type="image/gif",
                    url=None, video_url=None, camera="", label="", zone=""):
    """Edit an existing Discord webhook message to replace the image with a GIF."""
    if not webhook_url or not message_id:
        return "error: missing webhook URL or message ID"

    ext = "gif" if "gif" in (image_type or "") else ("mp4" if "video" in (image_type or "") else "jpg")

    embed = {
        "title": title,
        "description": message,
        "color": EMBED_COLOR,
        "timestamp": datetime.utcnow().isoformat(),
        "fields": [],
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

    payload = {"embeds": [embed], "attachments": []}

    files = {
        "payload_json": (None, json.dumps(payload), "application/json"),
    }
    if image_data:
        files["file"] = (f"preview.{ext}", image_data, image_type)

    try:
        patch_url = f"{webhook_url}/messages/{message_id}"
        resp = requests.patch(patch_url, files=files, timeout=30)
        if resp.status_code in (200, 204):
            log.info("Discord message %s updated with GIF", message_id)
            return "updated"
        else:
            log.error("Discord edit error: %s %s", resp.status_code, resp.text)
            return f"error: {resp.status_code}"
    except Exception as e:
        log.error("Discord edit exception: %s", e)
        return f"error: {e}"
