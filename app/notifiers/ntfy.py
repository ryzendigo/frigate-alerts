"""Ntfy notification provider — sends with image attachment."""

import logging
import requests

log = logging.getLogger("frigate-alerts")


def send_ntfy(config, title, message, image_data, image_type="image/gif", url=None):
    server = config.get("server", "https://ntfy.sh")
    topic = config.get("topic", "")
    if not topic:
        return "error: missing topic"

    ntfy_url = f"{server.rstrip('/')}/{topic}"
    ext = "gif" if "gif" in image_type else "jpg"

    headers = {
        "Title": title,
        "Filename": f"preview.{ext}",
    }
    if url:
        headers["Click"] = url
        headers["Actions"] = f"view, View in Frigate, {url}"

    token = config.get("token", "")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    try:
        resp = requests.put(ntfy_url, data=image_data, headers=headers, timeout=30)
        if resp.status_code == 200:
            log.info("Ntfy sent to %s/%s", server, topic)
            return "sent"
        else:
            log.error("Ntfy error: %s %s", resp.status_code, resp.text)
            return f"error: {resp.status_code}"
    except Exception as e:
        log.error("Ntfy exception: %s", e)
        return f"error: {e}"
