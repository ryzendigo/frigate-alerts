"""Telegram notification provider — sends GIF or photo via Bot API."""

import logging
import requests

log = logging.getLogger("frigate-alerts")


def send_telegram(config, title, message, image_data, image_type="image/gif", url=None):
    token = config.get("token", "")
    chat_id = config.get("chat_id", "")
    if not token or not chat_id:
        return "error: missing token or chat_id"

    text = f"*{title}*\n{message}"
    if url:
        text += f"\n[View in Frigate]({url})"

    api_base = f"https://api.telegram.org/bot{token}"

    try:
        if "gif" in image_type:
            resp = requests.post(
                f"{api_base}/sendAnimation",
                data={"chat_id": chat_id, "caption": text, "parse_mode": "Markdown"},
                files={"animation": ("preview.gif", image_data, image_type)},
                timeout=30,
            )
        else:
            resp = requests.post(
                f"{api_base}/sendPhoto",
                data={"chat_id": chat_id, "caption": text, "parse_mode": "Markdown"},
                files={"photo": ("snapshot.jpg", image_data, image_type)},
                timeout=30,
            )

        if resp.status_code == 200 and resp.json().get("ok"):
            log.info("Telegram sent to %s", chat_id)
            return "sent"
        else:
            log.error("Telegram error: %s", resp.text)
            return f"error: {resp.status_code}"
    except Exception as e:
        log.error("Telegram exception: %s", e)
        return f"error: {e}"
