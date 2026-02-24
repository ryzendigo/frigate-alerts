"""Generic webhook notification provider."""

import logging
import json
import requests

log = logging.getLogger("frigate-alerts")


def send_webhook(config, title, message, image_data, image_type="image/gif", url=None, event_data=None):
    webhook_url = config.get("url", "")
    method = config.get("method", "POST").upper()
    headers = config.get("headers", {})

    if not webhook_url:
        return "error: missing webhook URL"

    payload = {
        "title": title,
        "message": message,
        "url": url or "",
        "image_type": image_type,
        "event": event_data or {},
    }

    try:
        if method == "POST":
            resp = requests.post(webhook_url, json=payload, headers=headers, timeout=30)
        elif method == "PUT":
            resp = requests.put(webhook_url, json=payload, headers=headers, timeout=30)
        else:
            resp = requests.post(webhook_url, json=payload, headers=headers, timeout=30)

        if resp.status_code in (200, 201, 202, 204):
            log.info("Webhook sent to %s", webhook_url[:50])
            return "sent"
        else:
            log.error("Webhook error: %s %s", resp.status_code, resp.text[:200])
            return f"error: {resp.status_code}"
    except Exception as e:
        log.error("Webhook exception: %s", e)
        return f"error: {e}"
