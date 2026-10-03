import os
import httpx
from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv

from database import Base, engine, SessionLocal
from business import DEFAULT_BUSINESS_ID
import models
from routers import products, orders, payments

load_dotenv()

WHATSAPP_TOKEN = os.getenv("WHATSAPP_TOKEN")
WHATSAPP_PHONE_NUMBER_ID = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")
GRAPH_URL = f"https://graph.facebook.com/v21.0/{WHATSAPP_PHONE_NUMBER_ID}/messages"

Base.metadata.create_all(bind=engine)

with SessionLocal() as db:
    if not db.query(models.Business).filter(models.Business.id == DEFAULT_BUSINESS_ID).first():
        db.add(models.Business(id=DEFAULT_BUSINESS_ID, name="My Business"))
        db.commit()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(products.router)
app.include_router(orders.router)
app.include_router(payments.router)


@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403)


@app.post("/webhook")
async def receive_message(request: Request):
    body = await request.json()
    print("INCOMING:", body)

    try:
        entry = body["entry"][0]["changes"][0]["value"]
        messages = entry.get("messages")
        if messages:
            msg = messages[0]
            from_number = msg["from"]
            text = msg.get("text", {}).get("body", "")
            print(f"Message from {from_number}: {text}")
            await send_message(from_number, f"Got your message: {text}")
    except (KeyError, IndexError):
        pass

    return {"status": "ok"}


async def send_message(to: str, text: str):
    headers = {"Authorization": f"Bearer {WHATSAPP_TOKEN}"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "text",
        "text": {"body": text},
    }
    async with httpx.AsyncClient() as client:
        resp = await client.post(GRAPH_URL, headers=headers, json=payload)
        print("SEND RESPONSE:", resp.status_code, resp.text)
