"""Test setup: a throwaway SQLite DB, fixed secrets, and fakes for WhatsApp and Claude so tests
never send real messages or spend API credits."""
import os
import sys
import tempfile

_tmp = tempfile.mkdtemp(prefix="reone-tests-")
os.environ.update(
    DATABASE_URL=f"sqlite:///{_tmp}/test.db",
    SECRET_KEY="test-secret-key-test-secret-key!",
    SIGNUP_CODE="PILOT1",
    DASHBOARD_PASSWORD="owner-pass",
    WHATSAPP_PHONE_NUMBER_ID="ENV_PNID",
    WHATSAPP_TOKEN="env-token",
    WHATSAPP_VERIFY_TOKEN="verify-me",
    RAZORPAY_WEBHOOK_SECRET="legacy-webhook-secret",
    ANTHROPIC_API_KEY="test-key-not-used",
)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

import ai  # noqa: E402
import chatbot  # noqa: E402
import main  # noqa: E402
import whatsapp  # noqa: E402


class FakeWhatsApp:
    def __init__(self):
        self.sent = []  # (phone_number_id, to, type, text or template name)
        self.fail = False

    async def post(self, creds, payload):
        if creds is None or self.fail:
            raise whatsapp.WhatsAppSendError("fake failure")
        body = payload.get("text", {}).get("body") or payload.get("template", {}).get("name")
        self.sent.append((creds.phone_number_id, payload["to"], payload["type"], body))

    async def download(self, creds, media_id):
        return b"\xff\xd8fake-jpeg", "image/jpeg"


class FakeAI:
    """Returns queued AssistantTurns and records what the AI was shown."""

    def __init__(self):
        self.queue = []
        self.calls = []

    def turn(self, **overrides):
        base = dict(language="English", intent="order", reply="ok", cart=[], customer_name=None,
                    delivery_address=None, ready_to_place_order=False, needs_human=False)
        return ai.AssistantTurn(**{**base, **overrides})

    async def run_turn(self, shop_name, conversation, products, image=None, recent_orders=None):
        self.calls.append({"shop": shop_name, "products": [p.name for p in products], "image": image,
                           "orders": [o.id for o in recent_orders or []]})
        return self.queue.pop(0) if self.queue else self.turn()


@pytest.fixture(scope="session")
def client():
    return TestClient(main.app)


@pytest.fixture(autouse=True)
def fake_whatsapp(monkeypatch):
    fake = FakeWhatsApp()
    monkeypatch.setattr(whatsapp, "_post_message", fake.post)
    monkeypatch.setattr(chatbot, "download_media", fake.download)
    return fake


@pytest.fixture(autouse=True)
def fake_ai(monkeypatch):
    fake = FakeAI()
    monkeypatch.setattr(ai, "run_turn", fake.run_turn)
    return fake
