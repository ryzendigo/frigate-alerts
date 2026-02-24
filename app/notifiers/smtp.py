"""SMTP (email) notification provider — sends with image attachment."""

import logging
import smtplib
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

log = logging.getLogger("frigate-alerts")


def send_smtp(config, title, message, image_data, image_type="image/gif", url=None):
    server = config.get("server", "")
    port = config.get("port", 587)
    username = config.get("username", "")
    password = config.get("password", "")
    sender = config.get("from", username)
    recipients = config.get("recipients", [])

    if not server or not recipients:
        return "error: missing server or recipients"

    body = message
    if url:
        body += f"\n\nView in Frigate: {url}"

    msg = MIMEMultipart()
    msg["Subject"] = title
    msg["From"] = sender
    msg["To"] = ", ".join(recipients)
    msg.attach(MIMEText(body))

    ext = "gif" if "gif" in image_type else "jpeg"
    subtype = ext
    img = MIMEImage(image_data, _subtype=subtype)
    img.add_header("Content-Disposition", "attachment", filename=f"preview.{ext}")
    msg.attach(img)

    try:
        with smtplib.SMTP(server, port, timeout=30) as smtp:
            if config.get("tls", True):
                smtp.starttls()
            if username:
                smtp.login(username, password)
            smtp.send_message(msg)
        log.info("Email sent to %s", recipients)
        return "sent"
    except Exception as e:
        log.error("SMTP exception: %s", e)
        return f"error: {e}"
