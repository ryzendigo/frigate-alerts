"""Pushover notification provider. Supports animated GIF, snooze actions, per-camera overrides, silent follow-ups."""

import logging
import requests

log = logging.getLogger("frigate-alerts")

PUSHOVER_API = "https://api.pushover.net/1/messages.json"


def send_pushover(config, recipient, title, message, image_data, image_type="image/gif",
                   url=None, video_url=None, snooze_url=None, camera_overrides=None, silent=False):
    camera_overrides = camera_overrides or {}

    data = {
        "token": config["token"],
        "user": recipient["userkey"],
        "title": title,
        "message": message,
        "sound": "none" if silent else camera_overrides.get("sound", config.get("sound", "pushover")),
        "priority": str(camera_overrides.get("priority", config.get("priority", 0))),
        "html": "1",
    }

    priority = int(data["priority"])
    if priority == 2:
        data["retry"] = str(config.get("retry", 60))
        data["expire"] = str(config.get("expire", 3600))

    # Primary URL - Frigate review link
    if url:
        data["url"] = url
        data["url_title"] = "View in Frigate"

    # Supplementary URL for video page
    if video_url:
        data["message"] += f'\n\n<a href="{video_url}">Watch Video</a>'

    # Snooze action link (skip on silent follow-ups to reduce clutter)
    if snooze_url and not silent:
        data["message"] += f'\n<a href="{snooze_url}">Snooze 30 min</a>'

    # Attach image (GIF or snapshot - Pushover doesn't support video)
    files = None
    if image_data and "video" not in (image_type or ""):
        ext = "gif" if "gif" in image_type else "jpg"
        files = {"attachment": (f"preview.{ext}", image_data, image_type)}

    try:
        resp = requests.post(PUSHOVER_API, data=data, files=files, timeout=30)
        name = recipient.get("name", recipient["userkey"][:8])
        if resp.status_code == 200:
            log.info("Pushover %s to %s", "update sent" if silent else "sent", name)
            return "sent"
        else:
            log.error("Pushover error for %s: %s %s", name, resp.status_code, resp.text)
            return f"error: {resp.status_code}"
    except Exception as e:
        log.error("Pushover exception: %s", e)
        return f"error: {e}"
