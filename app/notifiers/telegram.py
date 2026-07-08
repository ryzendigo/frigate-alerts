"""Telegram notification provider. Sends GIF, photo, or video via Bot API."""

import html
import logging
import requests

log = logging.getLogger("frigate-alerts")


def send_telegram(config, title, message, media_data, media_type="image/gif",
                   url=None, video_url=None):
    token = config.get("token", "")
    chat_id = config.get("chat_id", "")
    if not token or not chat_id:
        return "error: missing token or chat_id"

    # HTML parse mode with escaping - Markdown breaks on _ * [ ] in camera/label names.
    # Truncate the raw text (before escaping/tags) so the caption stays under
    # Telegram's 1024-char limit without cutting an entity or tag mid-way.
    text = f"<b>{html.escape((title or '')[:200])}</b>\n{html.escape((message or '')[:800])}"
    links = []
    if url:
        links.append(f'<a href="{html.escape(url)}">View in Frigate</a>')
    if video_url:
        links.append(f'<a href="{html.escape(video_url)}">Watch Video</a>')
    if links:
        text += "\n\n" + " | ".join(links)

    api_base = f"https://api.telegram.org/bot{token}"

    try:
        if "video" in (media_type or ""):
            resp = requests.post(
                f"{api_base}/sendVideo",
                data={"chat_id": chat_id, "caption": text, "parse_mode": "HTML"},
                files={"video": ("clip.mp4", media_data, media_type)},
                timeout=60,
            )
        elif "gif" in (media_type or ""):
            resp = requests.post(
                f"{api_base}/sendAnimation",
                data={"chat_id": chat_id, "caption": text, "parse_mode": "HTML"},
                files={"animation": ("preview.gif", media_data, media_type)},
                timeout=30,
            )
        else:
            resp = requests.post(
                f"{api_base}/sendPhoto",
                data={"chat_id": chat_id, "caption": text, "parse_mode": "HTML"},
                files={"photo": ("snapshot.jpg", media_data, media_type)},
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
