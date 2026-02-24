"""Discord notification provider. Rich embeds with GIF/video attachments."""

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
        return "error: no webhook URL"

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
        resp = requests.post(webhook_url, files=files, timeout=30)
        name = webhook_config.get("name", "webhook")
        if resp.status_code in (200, 204):
            log.info("Discord sent to %s", name)
            return "sent"
        else:
            log.error("Discord error for %s: %s %s", name, resp.status_code, resp.text)
            return f"error: {resp.status_code}"
    except Exception as e:
        log.error("Discord exception: %s", e)
        return f"error: {e}"
