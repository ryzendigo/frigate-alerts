"""Pushover notification provider — supports animated GIF attachments."""

import logging
import requests

log = logging.getLogger("frigate-alerts")

PUSHOVER_API = "https://api.pushover.net/1/messages.json"


def send_pushover(config, recipient, title, message, image_data, image_type="image/gif", url=None):
    """Send notification with image to a Pushover recipient. Returns 'sent' or 'error'."""
    data = {
        "token": config["token"],
        "user": recipient["userkey"],
        "title": title,
        "message": message,
        "sound": config.get("sound", "pushover"),
        "priority": str(config.get("priority", 0)),
    }

    if config.get("priority") == 2:
        data["retry"] = str(config.get("retry", 60))
        data["expire"] = str(config.get("expire", 3600))

    if url:
        data["url"] = url
        data["url_title"] = "View in Frigate"

    ext = "gif" if "gif" in image_type else "jpg"
    files = {"attachment": (f"preview.{ext}", image_data, image_type)}

    try:
        resp = requests.post(PUSHOVER_API, data=data, files=files, timeout=30)
        name = recipient.get("name", recipient["userkey"][:8])
        if resp.status_code == 200:
            log.info("Pushover sent to %s", name)
            return "sent"
        else:
            log.error("Pushover error for %s: %s %s", name, resp.status_code, resp.text)
            return f"error: {resp.status_code}"
    except Exception as e:
        log.error("Pushover exception: %s", e)
        return f"error: {e}"
