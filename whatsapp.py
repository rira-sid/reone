import os

import httpx

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
GRAPH_URL = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"


class WhatsAppSendError(Exception):
    pass


async def send_message(to: str, text: str):
    """Send a text message; raises WhatsAppSendError if Meta rejects it or can't be reached."""
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.post(GRAPH_URL, headers=headers, json=payload)
    except httpx.HTTPError as e:
        raise WhatsAppSendError(f"Could not reach WhatsApp: {e!r}") from e
    print("SEND RESPONSE:", resp.status_code, resp.text)
    if resp.status_code >= 400:
        try:
            detail = resp.json()["error"]["message"]
        except (ValueError, KeyError, TypeError):
            detail = resp.text
        raise WhatsAppSendError(f"WhatsApp rejected the message: {detail}")
