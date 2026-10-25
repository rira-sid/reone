import hashlib
import hmac
import json

import main


def _sign(secret, raw):
    return "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()


def test_webhook_signature_checked_when_secret_set(client, monkeypatch):
    monkeypatch.setattr(main, "WHATSAPP_APP_SECRETS", ["s3cret", "other"])
    raw = json.dumps({"entry": []}).encode()
    assert client.post("/webhook", content=raw).status_code == 403
    assert client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": _sign("wrong", raw)}).status_code == 403
    assert client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": _sign("s3cret", raw)}).status_code == 200
    assert client.post("/webhook", content=raw, headers={"X-Hub-Signature-256": _sign("other", raw)}).status_code == 200


def test_webhook_open_without_secret(client, monkeypatch):
    monkeypatch.setattr(main, "WHATSAPP_APP_SECRETS", [])
    assert client.post("/webhook", json={"entry": []}).status_code == 200
