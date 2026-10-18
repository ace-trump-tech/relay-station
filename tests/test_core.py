import asyncio
import base64
import hashlib
import json
import os
import time
from datetime import datetime, timedelta, timezone

import pytest
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from fastapi.testclient import TestClient

from app.connectors import verify_feishu
from app.engine import DEFAULT_RULES, DEFAULTS, classify, propose, quiet_now
from app.main import create_app
from app.store import Store

HEADERS = {"Origin": "http://testserver", "X-Relay-Request": "1"}
PASSWORD = "correct-local-passphrase"


@pytest.fixture
def app(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("RELAY_SECURE_COOKIE", raising=False)
    monkeypatch.setenv("RELAY_TIMEZONE", "Asia/Shanghai")
    return create_app(tmp_path / "data", origin="http://testserver", background=False)


@pytest.fixture
def client(app):
    with TestClient(app) as client:
        client.headers.update(HEADERS)
        result = client.post("/api/setup", json={"password": PASSWORD})
        assert result.status_code == 200
        client.headers["X-CSRF-Token"] = result.json()["csrf"]
        yield client


def ingest(app, ident="one", title="紧急：请确认交付"):
    # These legacy message-delivery tests explicitly opt into raw-message alerts.
    app.state.store.set("organizer_profile", {"notify_messages": True})
    return app.state.ingest("qqmail", {"external_id": ident, "title": title,
        "sender": "test@example.test", "body": "请核对本周工作事项。这是保密正文。"})


def test_unauthenticated_data_is_protected(app):
    with TestClient(app) as anonymous:
        assert anonymous.get("/api/state").status_code == 401
        assert anonymous.get("/api/reminders").status_code == 401
        assert anonymous.get("/api/session").json()["needs_setup"]
        assert anonymous.get("/health").json()["ok"]
        assert anonymous.get("/").status_code == 200


def test_setup_csrf_logout_and_encrypted_password(app, client):
    assert client.get("/api/state").status_code == 200
    assert client.post("/api/setup", json={"password": PASSWORD}).status_code == 409
    assert client.post("/api/sync", headers={"Origin": "https://evil.test"}).status_code == 403
    assert client.post("/api/sync", headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert client.post("/api/logout").status_code == 200
    assert client.get("/api/state").status_code == 401
    assert client.get("/api/session").json()["needs_setup"] is False
    response = client.post("/api/login", json={"password": PASSWORD})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie
    assert PASSWORD.encode() not in app.state.store.path.read_bytes()


def test_login_rate_limit(app, client):
    client.post("/api/logout")
    for _ in range(5):
        assert client.post("/api/login", json={"password": "wrong-password-long"}).status_code == 401
    assert client.post("/api/login", json={"password": PASSWORD}).status_code == 429


def test_host_and_request_size(app, client):
    assert client.get("/api/state", headers={"Host": "attacker.test"}).status_code == 400
    assert client.post("/api/settings", content=b"a" * 256001).status_code == 413


def test_ingestion_is_idempotent_and_content_encrypted(app, client):
    assert ingest(app) == 1
    assert ingest(app) == 0
    assert len(client.get("/api/state").json()["messages"]) == 1
    raw = app.state.store.path.read_bytes()
    assert "保密正文".encode() not in raw
    assert b"test@example.test" not in raw
    store = Store(app.state.store.directory)
    assert store.messages()[0]["body"].endswith("保密正文。")


def test_missing_encryption_key_fails_closed(app):
    (app.state.store.directory / "vault.key").unlink()
    with pytest.raises(RuntimeError, match="missing"):
        Store(app.state.store.directory)


def test_settings_validation_and_no_secret_leak(app, client):
    assert client.put("/api/settings", json={**DEFAULTS, "work_end": "08:00"}).status_code == 422
    assert client.put("/api/settings", json={**DEFAULTS, "weekdays": []}).status_code == 422
    app.state.store.set("connector:feishu", {"encrypt_key": "secret-encrypt-key", "verification_token": "verify-token"})
    value = client.get("/api/state").text
    assert "secret-encrypt-key" not in value and "verify-token" not in value


def test_rule_order_and_disabled_rules():
    rules = json.loads(json.dumps(DEFAULT_RULES))
    assert classify("紧急会议", "", rules)["priority"] == "high"
    rules[0]["enabled"] = False
    assert classify("紧急会议", "", rules)["category"] == "会议"
    assert classify("Newsletter", "", rules)["priority"] == "low"


def test_scheduler_skips_weekend_lunch_busy_and_deadline():
    messages = [{"id": "one", "title": "确认工作", "priority": "high", "category": "工作", "source": "qqmail"},
                {"id": "two", "title": "过期事项", "priority": "normal", "category": "工作", "source": "qqmail", "deadline": "2026-10-17T09:00:00+08:00"},
                {"id": "three", "title": "Newsletter", "priority": "low", "category": "订阅", "source": "gmail"}]
    existing = [{"start": "2026-10-19T09:00:00+08:00", "end": "2026-10-19T12:00:00+08:00"}]
    result = propose(messages, existing, DEFAULTS, "Asia/Shanghai", datetime.fromisoformat("2026-10-18T11:00:00+08:00"))
    assert len(result["tasks"]) == 1
    assert result["tasks"][0]["start"] == "2026-10-19T13:00:00+08:00"
    assert result["unscheduled"][0]["message_id"] == "two"


def test_plan_commit_is_single_use_and_creates_persistent_reminders(app, client):
    ingest(app)
    proposal = client.post("/api/plan").json()
    assert len(proposal["tasks"]) == 1
    result = client.post("/api/plan/" + proposal["id"] + "/commit")
    assert result.status_code == 200
    assert client.post("/api/plan/" + proposal["id"] + "/commit").status_code == 409
    with app.state.store.connect() as db:
        assert db.execute("SELECT COUNT(*) FROM reminders WHERE task_id IS NOT NULL").fetchone()[0] == 1
    assert len(client.post("/api/plan").json()["tasks"]) == 0
    assert len(Store(app.state.store.directory).tasks()) == 1


def test_plan_commit_rechecks_new_conflicts_and_rolls_back(app, client):
    ingest(app)
    proposal = client.post("/api/plan").json()
    slot = proposal["tasks"][0]
    assert client.post("/api/tasks", json={"title": "手动占用", "start": slot["start"], "end": slot["end"]}).status_code == 200
    assert client.post("/api/plan/" + proposal["id"] + "/commit").status_code == 409
    assert len(client.get("/api/state").json()["tasks"]) == 1


def test_manual_tasks_validate_timezone_conflict_and_lifecycle(app, client):
    start = datetime.now(timezone.utc)+timedelta(hours=3)
    value = {"title": "专注时间", "start": start.isoformat(), "end": (start+timedelta(hours=1)).isoformat()}
    assert client.post("/api/tasks", json={**value, "start": "2026-10-19T10:00:00"}).status_code == 422
    result = client.post("/api/tasks", json=value)
    assert result.status_code == 200
    ident = result.json()["id"]
    assert client.post("/api/tasks", json=value).status_code == 409
    assert client.patch("/api/tasks/"+ident, json={"done": True}).status_code == 200
    assert client.get("/api/state").json()["tasks"][0]["done"]
    assert client.delete("/api/tasks/"+ident).status_code == 200
    assert not client.get("/api/state").json()["tasks"]


def test_quiet_hours_reminders_and_ack(app, client, monkeypatch):
    ingest(app)
    monkeypatch.setattr("app.main.quiet_now", lambda *_: True)
    app.state.store.set("settings", {**DEFAULTS, "quiet_start": "00:00", "quiet_end": "23:59"})
    assert client.get("/api/reminders").json() == []
    monkeypatch.setattr("app.main.quiet_now", lambda *_: False)
    app.state.store.set("settings", {**DEFAULTS, "quiet_start": "00:00", "quiet_end": "00:00"})
    reminders = client.get("/api/reminders").json()
    assert len(reminders) == 1
    assert client.post('/api/reminders/'+reminders[0]["id"]+'/ack').status_code == 200
    assert client.get("/api/reminders").json() == []
    assert quiet_now(DEFAULTS, datetime.fromisoformat("2026-10-18T23:00:00+08:00"))
    assert not quiet_now(DEFAULTS, datetime.fromisoformat("2026-10-18T13:00:00+08:00"))


def encrypted_feishu(config, event):
    key = hashlib.sha256(config["encrypt_key"].encode()).digest()
    iv = os.urandom(16)
    padder = padding.PKCS7(128).padder()
    value = padder.update(json.dumps(event).encode())+padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.CBC(iv)).encryptor()
    wire = iv+encryptor.update(value)+encryptor.finalize()
    raw = json.dumps({"encrypt": base64.b64encode(wire).decode()}).encode()
    timestamp, nonce = str(int(time.time())), "test-nonce"
    signature = hashlib.sha256((timestamp+nonce+config["encrypt_key"]).encode()+raw).hexdigest()
    return raw, {"x-lark-request-timestamp": timestamp, "x-lark-request-nonce": nonce, "x-lark-signature": signature}


def test_feishu_encryption_signature_expiry_and_replay(app, client):
    config = {"encrypt_key": "my-encrypt-key", "verification_token": "my-verification-token"}
    assert client.post("/api/connectors/feishu", json=config).status_code == 200
    event = {"schema": "2.0", "header": {"event_id": "evt1", "event_type": "im.message.receive_v1", "token": config["verification_token"]},
             "event": {"sender": {"sender_id": {"open_id": "ou_test"}}, "message": {"message_id": "m1", "message_type": "text", "content": json.dumps({"text": "紧急：确认会议"})}}}
    raw, headers = encrypted_feishu(config, event)
    assert verify_feishu(raw, headers, config) == event
    assert client.post("/webhooks/feishu", content=raw, headers=headers).status_code == 200
    assert client.post("/webhooks/feishu", content=raw, headers=headers).json()["duplicate"]
    assert len(app.state.store.messages()) == 1
    assert client.post("/webhooks/feishu", content=raw+b" ", headers=headers).status_code == 403
    with pytest.raises(ValueError, match="Expired"):
        verify_feishu(raw, headers, config, now=time.time()+600)
    challenge = {"type": "url_verification", "challenge": "abc", "token": config["verification_token"]}
    raw, headers = encrypted_feishu(config, challenge)
    assert client.post("/webhooks/feishu", content=raw, headers=headers).json() == {"challenge": "abc"}


def test_gmail_state_and_pkce_are_bound_to_session(app, client, monkeypatch):
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "example-client")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "example-secret")
    from urllib.parse import parse_qs, urlsplit
    result = client.post("/api/connectors/gmail/authorize").json()
    params = parse_qs(urlsplit(result["url"]).query)
    assert params["code_challenge_method"] == ["S256"]
    assert params["scope"] == ["https://www.googleapis.com/auth/gmail.readonly"]
    assert client.get("/oauth/google/callback?state=forged&code=bad").status_code == 400
    # State is consumed exactly once, including when authorization is denied.
    state = params["state"][0]
    response = client.get("/oauth/google/callback", params={"state": state, "error": "access_denied"}, follow_redirects=False)
    assert response.status_code == 303
    assert client.get("/oauth/google/callback", params={"state": state, "code": "bad"}).status_code == 400


def test_agent_opt_in_and_push_destination_validation(app, client):
    ingest(app)
    ident = app.state.store.messages()[0]["id"]
    assert client.post('/api/messages/'+ident+'/analyze').status_code == 403
    for url in ["http://localhost/private", "https://evil.test/open-apis/bot/v2/hook/abc", "https://open.feishu.cn.evil.test/open-apis/bot/v2/hook/abc"]:
        assert client.post('/api/push/feishu', json={"webhook":url,"secret":"long-secret"}).status_code == 422


def test_delivery_failure_retries_are_durable_and_backoff(app, client, monkeypatch):
    ingest(app)
    app.state.store.set("settings", {**DEFAULTS, "quiet_start":"00:00", "quiet_end":"00:00"})
    app.state.store.set("push:feishu", {"webhook":"unused", "secret":"unused"})
    calls = []
    async def failing_push(*args):
        calls.append(1)
        raise RuntimeError("Do not log provider details")
    monkeypatch.setattr("app.main.push_feishu", failing_push)
    asyncio.run(app.state.deliver_reminders())
    asyncio.run(app.state.deliver_reminders())
    assert len(calls) == 1
    with app.state.store.connect() as db:
        row = db.execute("SELECT * FROM deliveries").fetchone()
        assert row["attempts"] == 1 and row["next_try"] > time.time()
        db.execute("UPDATE deliveries SET next_try=0")
    async def success(*args):
        calls.append(2)
    monkeypatch.setattr("app.main.push_feishu", success)
    asyncio.run(app.state.deliver_reminders())
    asyncio.run(app.state.deliver_reminders())
    assert calls == [1, 2]
    with app.state.store.connect() as db:
        assert db.execute("SELECT delivered FROM deliveries").fetchone()[0] == 1


def test_message_read_ack_and_invalid_patches(app, client):
    ingest(app)
    ident = app.state.store.messages()[0]["id"]
    assert client.patch('/api/messages/'+ident, json={"priority":"execute-command"}).status_code == 422
    assert client.patch('/api/messages/'+ident, json={"read":None}).status_code == 422
    assert client.patch('/api/messages/'+ident, json={"deadline":"2026-10-19T10:00:00"}).status_code == 422
    assert client.patch('/api/messages/'+ident, json={"read":True}).status_code == 200
    with app.state.store.connect() as db:
        assert db.execute("SELECT acknowledged FROM reminders").fetchone()[0] == 1
