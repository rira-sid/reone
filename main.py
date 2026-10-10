import asyncio
import os
from collections import deque
from contextlib import asynccontextmanager

from dotenv import load_dotenv

# Load .env before importing modules that read env vars at import time (ai, whatsapp, payments).
load_dotenv()

from fastapi import BackgroundTasks, FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from database import Base, engine, SessionLocal
from business import DEFAULT_BUSINESS_ID, business_for_phone_number_id
import models
import auth
from chatbot import handle_incoming
from migrate import add_missing_columns
from reminders import reminder_loop
from routers import products, orders, payments, conversations, settings, invoices, insights, shop

WHATSAPP_VERIFY_TOKEN = os.getenv("WHATSAPP_VERIFY_TOKEN")

Base.metadata.create_all(bind=engine)
add_missing_columns(engine)

with SessionLocal() as db:
    if not db.query(models.Business).filter(models.Business.id == DEFAULT_BUSINESS_ID).first():
        db.add(models.Business(id=DEFAULT_BUSINESS_ID, name="My Business"))
        db.commit()

@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(reminder_loop())
    yield
    task.cancel()


app = FastAPI(lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173"],
    # Covers the production domain plus every Vercel preview deployment URL,
    # so a new preview build isn't blocked until someone remembers to add it here.
    allow_origin_regex=r"https://.*\.vercel\.app",
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(products.router)
app.include_router(orders.router)
app.include_router(payments.router)
app.include_router(conversations.router)
app.include_router(settings.router)
app.include_router(invoices.router)
app.include_router(insights.router)
app.include_router(shop.router)


@app.get("/health")
def health():
    """Uptime check, pinged by .github/workflows/keep-awake.yml so the free Render instance doesn't sleep."""
    return {"ok": True}


@app.get("/webhook")
def verify_webhook(request: Request):
    mode = request.query_params.get("hub.mode")
    token = request.query_params.get("hub.verify_token")
    challenge = request.query_params.get("hub.challenge")

    if mode == "subscribe" and token == WHATSAPP_VERIFY_TOKEN:
        return Response(content=challenge, media_type="text/plain")
    return Response(status_code=403)


# Meta retries webhook delivery (several times, same message id) if it doesn't get a fast
# 200 back - e.g. while a free-tier instance is waking up from sleep. Track recently-seen
# message ids so a retried delivery doesn't trigger a duplicate reply. Bounded deque so this
# can't grow unbounded; fine for a single instance - revisit if we ever scale out.
_SEEN_MESSAGE_IDS: deque[str] = deque(maxlen=1000)
_seen_message_id_set: set[str] = set()


def _already_processed(message_id: str) -> bool:
    if message_id in _seen_message_id_set:
        return True
    if len(_SEEN_MESSAGE_IDS) == _SEEN_MESSAGE_IDS.maxlen:
        _seen_message_id_set.discard(_SEEN_MESSAGE_IDS[0])
    _SEEN_MESSAGE_IDS.append(message_id)
    _seen_message_id_set.add(message_id)
    return False


# What gets stored/shown for message types the AI can't read as text.
_PLACEHOLDERS = {
    "audio": "[voice note]",
    "video": "[video]",
    "document": "[document]",
    "sticker": "[sticker]",
    "location": "[location pin]",
}


@app.post("/webhook")
async def receive_message(request: Request, background_tasks: BackgroundTasks):
    body = await request.json()
    print("INCOMING:", body)

    # One delivery can batch several messages, possibly for different sellers' numbers.
    for entry in body.get("entry", []):
        for change in entry.get("changes", []):
            value = change.get("value", {})
            messages = value.get("messages")
            if not messages:
                continue  # delivery/read status updates etc.

            phone_number_id = value.get("metadata", {}).get("phone_number_id")
            with SessionLocal() as db:
                business = business_for_phone_number_id(db, phone_number_id)
            if not business:
                print(f"No business connected to phone_number_id {phone_number_id}, ignoring")
                continue

            contacts = value.get("contacts") or [{}]
            profile_name = contacts[0].get("profile", {}).get("name")

            for msg in messages:
                message_id = msg.get("id")
                if message_id and _already_processed(message_id):
                    print(f"Duplicate delivery for {message_id}, skipping")
                    continue
                from_number = msg.get("from")
                if not from_number:
                    continue

                msg_type = msg.get("type")
                image_media_id = audio_media_id = None
                if msg_type == "text":
                    text = msg["text"]["body"]
                elif msg_type == "image":
                    image_media_id = msg["image"].get("id")
                    caption = msg["image"].get("caption")
                    text = f"[photo] {caption}" if caption else "[photo]"
                elif msg_type == "audio":
                    audio_media_id = msg["audio"].get("id")
                    text = "[voice note]"
                elif msg_type == "button":
                    text = msg["button"].get("text", "")
                else:
                    text = _PLACEHOLDERS.get(msg_type, f"[{msg_type or 'unsupported'} message]")
                print(f"Message for business {business.id} from {from_number}: {text}")
                # Reply in the background so we can return 200 to Meta immediately -
                # otherwise a slow cold-start response makes Meta assume delivery failed
                # and resend the same message, which is what caused the duplicate-reply bug.
                background_tasks.add_task(
                    handle_incoming, business.id, from_number, text, profile_name, image_media_id, message_id,
                    audio_media_id,
                )

    return {"status": "ok"}
