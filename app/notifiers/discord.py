"""Discord notification provider — supports animated GIF attachments."""

import logging
import requests

log = logging.getLogger("frigate-alerts")


def send_discord(webhook_config, title, message, image_data, image_type="image/gif", url=None):
    """Send notification with image to a Discord webhook. Returns 'sent' or 'error'."""
    webhook_url = webhook_config.get("url", "")
    if not webhook_url:
        return "error: no webhook URL"

    ext = "gif" if "gif" in image_type else "jpg"
    payload = {"content": f"**{title}**\n{message}"}
    if url:
        payload["content"] += f"\n[View in Frigate]({url})"

    files = {
        "payload_json": (None, __import__("json").dumps(payload), "application/json"),
        "file": (f"preview.{ext}", image_data, image_type),
    }

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
