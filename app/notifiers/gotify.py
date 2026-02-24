"""Gotify notification provider."""

import logging
import requests

log = logging.getLogger("frigate-alerts")


def send_gotify(config, title, message, image_data, image_type="image/gif", url=None):
    server = config.get("server", "")
    token = config.get("token", "")
    if not server or not token:
        return "error: missing server or token"

    body = message
    if url:
        body += f"\n\n[View in Frigate]({url})"

    try:
        resp = requests.post(
            f"{server.rstrip('/')}/message",
            params={"token": token},
            json={
                "title": title,
                "message": body,
                "priority": config.get("priority", 5),
                "extras": {"client::display": {"contentType": "text/markdown"}},
            },
            timeout=30,
        )
        if resp.status_code == 200:
            log.info("Gotify sent")
            return "sent"
        else:
            log.error("Gotify error: %s %s", resp.status_code, resp.text)
            return f"error: {resp.status_code}"
    except Exception as e:
        log.error("Gotify exception: %s", e)
        return f"error: {e}"
