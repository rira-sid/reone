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
    GEO_LOOKUP="0",
    BATCH_WAIT_SECONDS="0",
    BATCH_WAIT_LONG_SECONDS="0",
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
        self.sent = []  # (phone_number_id, to, type, text / template name / audio id / interactive type)
        self.payloads = []
        self.fail = False

    async def post(self, creds, payload):
        if creds is None or self.fail:
            raise whatsapp.WhatsAppSendError("fake failure")
        body = (payload.get("text", {}).get("body") or payload.get("template", {}).get("name")
                or payload.get("reaction", {}).get("emoji") or payload.get("interactive", {}).get("type"))
        self.payloads.append(payload)
        self.sent.append((creds.phone_number_id, payload["to"], payload["type"], body))
        return f"wamid.out.{len(self.sent)}"

    async def download(self, creds, media_id):
        if media_id.startswith("AUDIO"):
            return b"OggSfake-voice", "audio/ogg; codecs=opus"
        return b"\xff\xd8fake-jpeg", "image/jpeg"


class FakeAI:
    """Returns queued AssistantTurns and records what the AI was shown."""

    def __init__(self):
        self.queue = []
        self.calls = []

    def turn(self, **overrides):
        base = dict(intent="question", reply="", cart=[])
        if overrides.get("intent") == "shop":
            base["share_link"] = True  # a customer asking to order/browse gets the shop link
        return ai.AssistantTurn(**{**base, **overrides})

    async def run_turn(self, shop_name, conversation, products, image=None, recent_orders=None, policies="",
                       audio=None, language="English", profile="", open_items="", welcome_note="", new_after=None):
        import ai as _ai

        self.calls.append({"shop": shop_name, "products": [p.name for p in products], "image": image,
                           "orders": [o.id for o in recent_orders or []], "policies": policies, "audio": audio,
                           "language": language, "profile": profile, "open_items": open_items,
                           "welcome_note": welcome_note, "transcript": _ai._transcript(conversation, new_after)})
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


@pytest.fixture(autouse=True)
def fake_translator(monkeypatch):
    """Product translations without calling Gemini: "[ta] Chicken Masala"."""
    import catalog_i18n

    calls = []

    async def ask(source, shop_name):
        calls.append(source["name"])
        return {code: catalog_i18n._LangText(
            lang=code, name=f"[{code}] {source['name']}", description=f"[{code}] {source['description']}" if source["description"] else "",
            category=f"[{code}] {source['category']}" if source["category"] else "", nutrition=source["nutrition"],
            sizes=[f"[{code}] {s}" for s in source["sizes"]]) for code in catalog_i18n.TARGETS}

    monkeypatch.setattr(catalog_i18n, "_ask_gemini", ask)
    return calls
