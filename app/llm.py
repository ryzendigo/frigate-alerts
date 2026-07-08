"""Optional LLM vision descriptions.

Sends the event snapshot to an OpenAI-compatible chat/vision endpoint and returns
a short natural-language description to enrich the notification. Compatible with
Ollama, LocalAI, vLLM, LM Studio, OpenAI, etc. (anything exposing
POST {base_url}/chat/completions with image_url content).

Fail-open by design: any error/timeout returns None and the caller sends the
normal notification, so the alert is never blocked or dropped.
"""

import base64
import logging

import requests

log = logging.getLogger("frigate-alerts")

DEFAULT_PROMPT = (
    "You are a security camera assistant. In one short sentence, describe what "
    "the person or object in this image appears to be doing. Be concise and factual."
)


def describe_image(cfg, image_bytes, context="", session=None):
    """Return a one-line description of the snapshot, or None on any failure.

    cfg keys: base_url (required), model (required), api_key, prompt, timeout,
    max_tokens.
    """
    base_url = (cfg.get("base_url") or "").rstrip("/")
    model = cfg.get("model") or ""
    if not base_url or not model or not image_bytes:
        return None

    b64 = base64.b64encode(image_bytes).decode("ascii")
    prompt = cfg.get("prompt") or DEFAULT_PROMPT
    if context:
        prompt = f"{prompt} Context: {context}."

    payload = {
        "model": model,
        "temperature": 0,
        "max_tokens": int(cfg.get("max_tokens", 60)),
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
                ],
            }
        ],
    }
    headers = {"Content-Type": "application/json"}
    if cfg.get("api_key"):
        headers["Authorization"] = f"Bearer {cfg['api_key']}"

    try:
        _post = session.post if session else requests.post
        resp = _post(f"{base_url}/chat/completions", json=payload, headers=headers,
                     timeout=cfg.get("timeout", 10))
        if resp.status_code != 200:
            log.warning("LLM description failed: %s %s", resp.status_code, resp.text[:200])
            return None
        data = resp.json()
        desc = (data["choices"][0]["message"]["content"] or "").strip()
        # collapse to a single line
        desc = " ".join(desc.split())
        return desc or None
    except Exception as e:
        log.warning("LLM description error: %s", e)
        return None
