import asyncio
import base64
import time

from app.connectors import gmail_sync, qq_sync
from app.store import Store


def test_qq_is_readonly_filters_old_uids_and_advances_cursor(monkeypatch):
    calls = []
    raw = b"Subject: Read-only import\r\nFrom: user@example.test\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nHello inbox"
    class FakeIMAP:
        def __init__(self, host, port, **kwargs):
            assert host == "imap.qq.com" and port == 993
            assert kwargs["ssl_context"].check_hostname
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def login(self, user, password):
            assert user == "example@qq.com" and password == "test-authorization-code"
        def select(self, folder, readonly):
            assert folder == "INBOX" and readonly
            return "OK", [b"3"]
        def response(self, name): return "UIDVALIDITY", [b"88"]
        def uid(self, command, *args):
            calls.append((command, args))
            if command == "search": return "OK", [b"7 8 9"]
            if args[1] == "(RFC822.SIZE)": return "OK", [b"8 (RFC822.SIZE 150)"]
            assert args[1] == "(BODY.PEEK[])"
            return "OK", [(b"8 BODY[]", raw)]
    monkeypatch.setattr("app.connectors.imaplib.IMAP4_SSL", FakeIMAP)
    messages, cursor = qq_sync({"email":"example@qq.com", "authorization_code":"test-authorization-code"}, {"validity":"88", "uid":7})
    assert [m["external_id"] for m in messages] == ["example@qq.com:88:8", "example@qq.com:88:9"]
    assert messages[0]["body"] == "Hello inbox"
    assert cursor == {"validity":"88", "uid":9}
    assert calls[0][1][-1] == "UID 8:*"


def test_gmail_refresh_readonly_scope_and_account_namespacing(monkeypatch, tmp_path):
    store = Store(tmp_path)
    store.set("connector:gmail", {"access_token":"expired", "refresh_token":"refresh-secret", "expires_at":0, "mailbox_id":"one@example.test"})
    calls = []
    class Reply:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def post(self, url, data):
            assert url == "https://oauth2.googleapis.com/token"
            assert data["grant_type"] == "refresh_token"
            calls.append("refresh")
            return Reply({"access_token":"new-token", "expires_in":3600})
        async def get(self, url, params, headers):
            assert headers["Authorization"] == "Bearer new-token"
            calls.append(url)
            if url.endswith("/messages"):
                assert params["labelIds"] == "INBOX"
                return Reply({"messages":[{"id":"abc"}]})
            return Reply({"payload":{"mimeType":"text/plain", "headers":[{"name":"Subject","value":"Test"},{"name":"From","value":"sender@test"}], "body":{"data":base64.urlsafe_b64encode(b"plain content").decode()}}, "snippet":"fallback"})
    monkeypatch.setattr("app.connectors.httpx.AsyncClient", Client)
    messages, cursor = asyncio.run(gmail_sync(store, "client", "secret"))
    assert messages[0]["external_id"] == "one@example.test:abc"
    assert messages[0]["body"] == "plain content"
    assert calls[0] == "refresh"
    assert store.get("connector:gmail")["refresh_token"] == "refresh-secret"
    assert cursor["since"] <= int(time.time())


def test_gmail_large_backlog_resumes_next_page(monkeypatch, tmp_path):
    store = Store(tmp_path)
    store.set("connector:gmail", {"access_token":"test", "refresh_token":"test", "expires_at":time.time()+3600, "mailbox_id":"one@test"})
    seen = []
    class Reply:
        def __init__(self, data): self.data = data
        def raise_for_status(self): pass
        def json(self): return self.data
    class Client:
        def __init__(self, **kwargs): pass
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def get(self, url, params, headers):
            page = int(params.get("pageToken", "0"))
            seen.append(page)
            return Reply({"messages":[], "nextPageToken":str(page+1)})
    monkeypatch.setattr("app.connectors.httpx.AsyncClient", Client)
    _, cursor = asyncio.run(gmail_sync(store, "client", "secret"))
    assert cursor["page_token"] == "10"
    store.set("cursor:gmail", cursor)
    _, next_cursor = asyncio.run(gmail_sync(store, "client", "secret"))
    assert seen == list(range(20))
    assert next_cursor["page_token"] == "20" and next_cursor["since"] == cursor["since"]
