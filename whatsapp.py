"""WhatsApp Cloud API calls, always made with the sending business's own credentials."""
import httpx

from business import WhatsAppCreds

GRAPH_BASE = "https://graph.facebook.com/v21.0"


class WhatsAppSendError(Exception):
    pass


def _error_detail(resp: httpx.Response) -> str:
    try:
        return resp.json()["error"]["message"]
    except (ValueError, KeyError, TypeError):
        return resp.text


async def _post_message(creds: WhatsAppCreds | None, payload: dict):
    if creds is None:
        raise WhatsAppSendError("WhatsApp is not connected for this business")
    headers = {"Authorization": f"Bearer {creds.token}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(f"{GRAPH_BASE}/{creds.phone_number_id}/messages", headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise WhatsAppSendError(f"Could not reach WhatsApp: {e!r}") from e
    print("SEND RESPONSE:", resp.status_code, resp.text)
    if resp.status_code >= 400:
        raise WhatsAppSendError(f"WhatsApp rejected the message: {_error_detail(resp)}")


async def send_message(creds: WhatsAppCreds | None, to: str, text: str):
    """Send a free-form text. Only works within 24h of the customer's last message.
    Raises WhatsAppSendError if Meta rejects it or can't be reached."""
    await _post_message(creds, {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text}})


async def send_template(creds: WhatsAppCreds | None, to: str, name: str, language: str, params: list[str]):
    """Send a Meta-approved template - the only kind of message allowed outside the 24h window."""
    await _post_message(creds, {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": name,
            "language": {"code": language},
            "components": [{"type": "body", "parameters": [{"type": "text", "text": p} for p in params]}],
        },
    })


async def send_interactive(creds: WhatsAppCreds | None, to: str, interactive: dict):
    """Send an interactive message (button, order details...). Only within the 24h window."""
    await _post_message(creds, {"messaging_product": "whatsapp", "recipient_type": "individual", "to": to,
                                "type": "interactive", "interactive": interactive})


async def send_audio(creds: WhatsAppCreds | None, to: str, data: bytes, mime_type: str = "audio/mpeg"):
    """Upload an audio file to WhatsApp and send it to the customer as a playable audio message."""
    media_id = await upload_media(creds, data, mime_type, "reply.mp3")
    await _post_message(creds, {"messaging_product": "whatsapp", "to": to, "type": "audio", "audio": {"id": media_id}})


async def upload_media(creds: WhatsAppCreds | None, data: bytes, mime_type: str, filename: str) -> str:
    """Upload a file to WhatsApp's media store and return its media id."""
    if creds is None:
        raise WhatsAppSendError("WhatsApp is not connected for this business")
    headers = {"Authorization": f"Bearer {creds.token}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{GRAPH_BASE}/{creds.phone_number_id}/media",
                headers=headers,
                data={"messaging_product": "whatsapp", "type": mime_type},
                files={"file": (filename, data, mime_type)},
            )
    except httpx.HTTPError as e:
        raise WhatsAppSendError(f"Could not reach WhatsApp: {e!r}") from e
    if resp.status_code >= 400:
        raise WhatsAppSendError(f"WhatsApp rejected the upload: {_error_detail(resp)}")
    return resp.json()["id"]


async def lookup_payment(creds: WhatsAppCreds | None, configuration: str, reference_id: str) -> dict | None:
    """Ask Meta for the real state of a WhatsApp Pay order (never trust a webhook alone).
    Returns the payment object, or None if Meta has no record of it."""
    if creds is None:
        raise WhatsAppSendError("WhatsApp is not connected for this business")
    headers = {"Authorization": f"Bearer {creds.token}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{GRAPH_BASE}/{creds.phone_number_id}/payments/{configuration}/{reference_id}",
                                    headers=headers)
    except httpx.HTTPError as e:
        raise WhatsAppSendError(f"Could not reach WhatsApp: {e!r}") from e
    if resp.status_code >= 400:
        raise WhatsAppSendError(f"Payment lookup failed: {_error_detail(resp)}")
    payments = resp.json().get("payments") or []
    return payments[0] if payments else None


async def download_media(creds: WhatsAppCreds | None, media_id: str) -> tuple[bytes, str]:
    """Fetch an image/voice note a customer sent. Returns (bytes, mime_type)."""
    if creds is None:
        raise WhatsAppSendError("WhatsApp is not connected for this business")
    headers = {"Authorization": f"Bearer {creds.token}"}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            meta = await client.get(f"{GRAPH_BASE}/{media_id}", headers=headers)
            if meta.status_code >= 400:
                raise WhatsAppSendError(f"Could not look up media: {_error_detail(meta)}")
            info = meta.json()
            media = await client.get(info["url"], headers=headers)
            if media.status_code >= 400:
                raise WhatsAppSendError(f"Could not download media ({media.status_code})")
    except httpx.HTTPError as e:
        raise WhatsAppSendError(f"Could not reach WhatsApp: {e!r}") from e
    return media.content, info.get("mime_type", "application/octet-stream")
