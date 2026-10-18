import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from desktop import InstanceLock, user_data_dir


def test_desktop_data_path_is_outside_program_directory(monkeypatch, tmp_path):
    if os.name == "nt":
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        assert user_data_dir() == tmp_path / "RelayStation"
    else:
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert user_data_dir() == tmp_path / "relay-station"


def test_instance_lock_is_exclusive_and_released(tmp_path):
    first = InstanceLock(tmp_path)
    if os.name == "nt":
        with pytest.raises(RuntimeError, match="Relay"):
            InstanceLock(tmp_path)
    first.close()
    InstanceLock(tmp_path).close()


def test_service_credentials_are_encrypted_hidden_and_immediately_effective(tmp_path, monkeypatch):
    monkeypatch.delenv("RELAY_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)
    app = create_app(tmp_path, origin="http://testserver", background=False)
    with TestClient(app) as client:
        assert client.get("/api/services").status_code == 401
        client.headers.update({"Origin":"http://testserver", "X-Relay-Request":"1"})
        result = client.post("/api/setup", json={"password":"local-desktop-test-password"})
        client.headers["X-CSRF-Token"] = result.json()["csrf"]
        assert not client.get("/api/state").json()["google_configured"]
        payload = {"google_client_id":"desktop-client", "google_client_secret":"private-client-secret",
                   "agent_base_url":"https://provider.example/v1", "agent_api_key":"private-agent-key", "agent_model":"example-model"}
        result = client.put("/api/services", json=payload)
        assert result.status_code == 200
        assert result.json()["google_client_secret_set"] and result.json()["agent_api_key_set"]
        assert "private-client-secret" not in result.text and "private-agent-key" not in result.text
        assert client.get("/api/state").json()["google_configured"]
        assert client.get("/api/state").json()["agent_configured"]
        assert client.post("/api/connectors/gmail/authorize").status_code == 200
        payload.update(google_client_secret="", agent_api_key="")
        assert client.put("/api/services", json=payload).status_code == 200
        assert app.state.store.get("services")["agent_api_key"] == "private-agent-key"
        assert b"private-client-secret" not in app.state.store.path.read_bytes()
        assert b"private-agent-key" not in app.state.store.path.read_bytes()
        assert client.put("/api/services", json={**payload,"agent_base_url":"http://insecure.example/v1"}).status_code == 422
        assert client.put("/api/services", json={**payload,"agent_base_url":"https://user:pass@provider.example/v1"}).status_code == 422
    # The encrypted settings persist across app recreation without env variables.
    restarted = create_app(tmp_path, origin="http://testserver", background=False)
    assert restarted.state.store.get("services")["google_client_id"] == "desktop-client"
