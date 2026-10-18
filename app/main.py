"""Single-user self-hosted API and background worker.

Run exactly one Uvicorn worker. SQLite transactions serialize plan commits;
the polling worker owns mailbox catch-up and durable notification retries.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import re
import secrets
import time
import uuid
from contextlib import asynccontextmanager, suppress
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit
from zoneinfo import ZoneInfo

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .connectors import agent_analyze, gmail_sync, push_feishu, qq_sync, verify_feishu
from .engine import DEFAULT_RULES, DEFAULTS, classify, propose, quiet_now
from .store import Store
from . import __version__
from .organizer import Organizer, profile as organizer_profile
from .organizer_api import register as register_organizer
from .canvas import Canvas

ROOT = Path(__file__).resolve().parent.parent
PRIORITIES = ("high", "normal", "low")
CATEGORIES = ("工作", "会议", "财务", "订阅", "生活")


class Login(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class Settings(BaseModel):
    work_start: str = "09:00"
    work_end: str = "18:00"
    lunch_start: str = "12:00"
    lunch_end: str = "13:00"
    weekdays: list[int] = Field(default_factory=lambda: [0, 1, 2, 3, 4], min_length=1, max_length=7)
    duration: int = Field(default=30, ge=5, le=240)
    remind_minutes: int = Field(default=10, ge=0, le=1440)
    quiet_start: str = "22:00"
    quiet_end: str = "08:00"
    agent_enabled: bool = False
    auto_plan: bool = False
    desktop_notifications: bool = False

    @field_validator("work_start", "work_end", "lunch_start", "lunch_end", "quiet_start", "quiet_end")
    @classmethod
    def clock(cls, value):
        if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
            raise ValueError("使用 HH:MM 时间格式")
        return value

    @model_validator(mode="after")
    def valid_ranges(self):
        if not self.work_start < self.work_end or not self.lunch_start <= self.lunch_end:
            raise ValueError("结束时间必须晚于开始时间")
        if any(d < 0 or d > 6 for d in self.weekdays):
            raise ValueError("工作日必须在 0 到 6 之间")
        return self


class Rule(BaseModel):
    id: str = Field(max_length=64)
    name: str = Field(min_length=1, max_length=80)
    keywords: str = Field(min_length=1, max_length=1000)
    priority: str
    category: str
    enabled: bool = True

    @field_validator("priority")
    @classmethod
    def priority_valid(cls, value):
        if value not in PRIORITIES:
            raise ValueError("未知优先级")
        return value

    @field_validator("category")
    @classmethod
    def category_valid(cls, value):
        if value not in CATEGORIES:
            raise ValueError("未知分类")
        return value


class MessagePatch(BaseModel):
    read: bool | None = None
    archived: bool | None = None
    priority: str | None = None
    category: str | None = None
    deadline: datetime | None = None


class TaskInput(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    start: datetime
    end: datetime
    message_id: str | None = Field(default=None, max_length=64)
    item_id: str | None = Field(default=None, max_length=64)
    priority: str = "normal"
    source: str = "manual"

    @model_validator(mode="after")
    def valid_times(self):
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("时间必须包含时区")
        if not self.start < self.end or self.end - self.start > timedelta(days=1):
            raise ValueError("日程时长必须在 0 到 24 小时之间")
        if self.priority not in PRIORITIES:
            raise ValueError("未知优先级")
        return self


class QQConfig(BaseModel):
    email: str = Field(pattern=r"^[\w.+-]+@(qq|foxmail)\.com$", max_length=254)
    authorization_code: str = Field(min_length=8, max_length=128)


class FeishuConfig(BaseModel):
    encrypt_key: str = Field(min_length=8, max_length=256)
    verification_token: str = Field(min_length=8, max_length=256)


class PushConfig(BaseModel):
    webhook: str = Field(min_length=50, max_length=300)
    secret: str = Field(min_length=8, max_length=256)

    @field_validator("webhook")
    @classmethod
    def valid_url(cls, value):
        parsed = urlsplit(value)
        if not re.fullmatch(r"https://open\.feishu\.cn/open-apis/bot/v2/hook/[\w-]+", value):
            raise ValueError("仅允许官方飞书机器人 HTTPS Webhook")
        return value


class ServiceConfig(BaseModel):
    google_client_id: str = Field(default="", max_length=512)
    google_client_secret: str = Field(default="", max_length=4096)
    agent_base_url: str = Field(default="", max_length=1000)
    agent_api_key: str = Field(default="", max_length=8192)
    agent_model: str = Field(default="", max_length=200)

    @field_validator("agent_base_url")
    @classmethod
    def https_endpoint(cls, value):
        if not value:
            return value
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Agent 地址须为不含用户名、查询参数的 HTTPS 基础地址")
        return value.rstrip("/")


def password_hash(password, salt):
    return hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=16384, r=8, p=1).hex()


def create_app(data_dir=None, origin=None, background=True):
    store = Store(data_dir or os.getenv("RELAY_DATA_DIR", str(ROOT / "data")))
    origin = (origin or os.getenv("RELAY_ORIGIN", "http://localhost:8765")).rstrip("/")
    parsed_origin = urlsplit(origin)
    if parsed_origin.scheme not in ("http", "https") or not parsed_origin.hostname or parsed_origin.path or parsed_origin.query or parsed_origin.fragment or parsed_origin.username:
        raise RuntimeError("RELAY_ORIGIN must be an http(s) origin without a path")
    if parsed_origin.scheme == "http" and parsed_origin.hostname not in ("localhost", "127.0.0.1", "testserver"):
        raise RuntimeError("Remote deployment requires an HTTPS origin")
    timezone_name = os.getenv("RELAY_TIMEZONE", "Asia/Shanghai")
    tz = ZoneInfo(timezone_name)
    secure_cookie = parsed_origin.scheme == "https" or os.getenv("RELAY_SECURE_COOKIE") == "1"
    sessions, pending_oauth, login_attempts = {}, {}, {}
    sync_lock = asyncio.Lock()
    bootstrap = os.getenv("RELAY_ADMIN_PASSWORD", "")
    if bootstrap and not store.get("password"):
        if len(bootstrap) < 12:
            raise RuntimeError("RELAY_ADMIN_PASSWORD must contain at least 12 characters")
        salt = secrets.token_hex(16)
        store.set("password", {"salt": salt, "hash": password_hash(bootstrap, salt)})

    def now():
        return datetime.now(tz)

    def settings():
        return dict(DEFAULTS, **store.get("settings", {}))

    def service_value(key):
        return store.get("services", {}).get(key.lower(), os.getenv(key.upper(), ""))

    def public_services():
        return {"google_client_id": service_value("google_client_id"),
                "google_client_secret_set": bool(service_value("google_client_secret")),
                "agent_base_url": service_value("agent_base_url"),
                "agent_model": service_value("agent_model"),
                "agent_api_key_set": bool(service_value("agent_api_key"))}

    organizer=Organizer(store,timezone_name,settings)

    def set_status(source, state, error=None):
        store.set("status:" + source, {"state": state, "last_sync": now().isoformat(), "error": error})

    def ingest(source, message):
        ident = uuid.uuid4().hex
        timestamp = now().isoformat()
        message = {"id": ident, "source": source, "external_id": message["external_id"],
                   "title": message["title"][:500], "body": message["body"][:30000],
                   "sender": message["sender"][:500], "created_at": timestamp,
                   "read": False, "archived": False,
                   **classify(message["title"], message["body"], store.get("rules", DEFAULT_RULES))}
        with store.connect() as db:
            count = db.execute("INSERT OR IGNORE INTO messages VALUES (?,?,?,?,?)", (
                ident, source, message["external_id"], timestamp, store.encrypt(message))).rowcount
            if count and message["priority"] == "high" and organizer_profile(store)["notify_messages"]:
                reminder = {"id": "msg:"+ident, "title": "重点消息：" + message["title"], "source": source,
                            "due": timestamp, "message_id": ident, "kind": "message"}
                db.execute("INSERT INTO reminders (id,due,payload) VALUES (?,?,?)",
                           (reminder["id"], timestamp, store.encrypt(reminder)))
        if count:
            organizer.capture(message)
        return count

    def save_tasks(values):
        saved = []
        with store.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = store.tasks()
            message_ids = {m["id"] for m in store.messages()}
            items={i["id"]:i for i in store.items()}
            for value in values:
                data = TaskInput(**value)
                # Store all schedule indexes in UTC so lexicographic queries are reliable.
                start = data.start.astimezone(timezone.utc).isoformat()
                end = data.end.astimezone(timezone.utc).isoformat()
                if data.message_id and data.message_id not in message_ids:
                    raise HTTPException(404, "关联消息不存在")
                if data.item_id and data.item_id not in items:
                    raise HTTPException(404,"关联事项不存在")
                if data.item_id:
                    item=items[data.item_id]
                    if item["status"] not in ("todo","doing") or item["verification"]!="confirmed" or not item["include_in_plan"]:
                        raise HTTPException(409,"事项已变更，请重新确认并生成计划")
                    if (data.end-data.start).total_seconds()!=item["estimated_minutes"]*60 or data.title!=item["title"]:
                        raise HTTPException(409,"事项内容或准备时长已变更，请重新生成计划")
                    if item.get("due_at") and data.end>datetime.fromisoformat(item["due_at"])-timedelta(minutes=item["buffer_minutes"]):
                        raise HTTPException(409,"该安排超出事项截止缓冲，请重新生成计划")
                for block in store.blocks():
                    if block["enabled"] and data.start<datetime.fromisoformat(block["end"]) and data.end>datetime.fromisoformat(block["start"]):
                        raise HTTPException(409,"该时段有课程或固定安排，请调整时间")
                for task in existing + saved:
                    if data.item_id and data.item_id==task.get("item_id"):
                        raise HTTPException(409,"这项备忘已安排日程")
                    if data.message_id and data.message_id == task.get("message_id"):
                        raise HTTPException(409, "这条消息已安排日程")
                    if task.get("done"):
                        continue
                    if data.start < datetime.fromisoformat(task["end"]) and data.end > datetime.fromisoformat(task["start"]):
                        raise HTTPException(409, "时间已被占用，请重新生成计划或调整时间")
                task = dict(data.model_dump(mode="json"), id=uuid.uuid4().hex, start=start, end=end, done=False)
                db.execute("INSERT INTO tasks (id,message_id,item_id,start,end,payload) VALUES (?,?,?,?,?,?)", (
                    task["id"], data.message_id, data.item_id, start, end, store.encrypt(task)))
                due = (data.start - timedelta(minutes=settings()["remind_minutes"])).astimezone(timezone.utc).isoformat()
                reminder = {"id": "task:"+task["id"], "task_id": task["id"], "title": "日程提醒："+task["title"],
                            "due": due, "source": data.source, "kind": "task", "item_id":data.item_id}
                db.execute("INSERT INTO reminders (id,task_id,due,payload) VALUES (?,?,?,?)", (
                    reminder["id"], task["id"], due, store.encrypt(reminder)))
                saved.append(task)
        return saved

    async def sync_sources():
        if sync_lock.locked():
            return {"busy": True}
        counts = {}
        async with sync_lock:
            for source in ("qqmail", "gmail", "canvas"):
                if not organizer_profile(store)["sources"].get(source,{}).get("enabled",True):
                    continue
                config = store.get("connector:" + source)
                if not config:
                    continue
                try:
                    if source=="canvas":
                        effective={**config,"sync_assignments":config.get("sync_assignments",True) and organizer_profile(store)["sources"][source]["capture"]}
                        result=await Canvas(effective).sync()
                        for assignment in result["assignments"]:
                            organizer.canvas_assignment(assignment)
                        for block in result["blocks"]:
                            organizer.upsert_block(block,block["external_key"])
                        if config.get("sync_calendar") and not result["truncated"]:
                            from datetime import timezone as utc_timezone
                            current=datetime.now(utc_timezone.utc)
                            keys={b["external_key"] for b in result["blocks"]}
                            for old in store.blocks():
                                if old["external_key"].startswith("canvas:"+config["base_url"]+":") and current<=datetime.fromisoformat(old["start"])<=current+timedelta(days=14) and old["external_key"] not in keys:
                                    organizer.upsert_block({**old,"enabled":False},old["external_key"])
                        counts[source]=len(result["assignments"])
                        set_status(source,"connected","本轮达到分页或课程数量上限，请缩小课程范围" if result["truncated"] else None)
                        continue
                    if source == "qqmail":
                        messages, cursor = await asyncio.to_thread(qq_sync, config, store.get("cursor:qqmail"))
                    else:
                        messages, cursor = await gmail_sync(store, service_value("google_client_id"), service_value("google_client_secret"))
                    counts[source] = sum(ingest(source, message) for message in messages)
                    store.set("cursor:" + source, cursor)
                    set_status(source, "connected")
                except Exception:
                    # Provider exceptions may contain secrets or email text; never log them.
                    counts[source] = 0
                    set_status(source, "error", "同步失败，请检查网络、授权状态和平台权限后重试")
        return counts

    def due_reminders():
        current_settings = settings()
        if quiet_now(current_settings, now()):
            return []
        with store.connect() as db:
            rows = db.execute("SELECT payload FROM reminders WHERE acknowledged=0 ORDER BY due").fetchall()
        items={i["id"]:i for i in store.items()}
        due=[]
        for row in rows:
            reminder=store.decrypt(row[0])
            if datetime.fromisoformat(reminder["due"])>now():
                continue
            if reminder.get("item_id"):
                item=items.get(reminder["item_id"])
                if not item or item["status"] in ("done","dismissed","inbox") or item["verification"]!="confirmed" or item["remind_mode"]!="deadline":
                    continue
                reminder["notify_feishu"]=item.get("notify_feishu",False)
            due.append(reminder)
        return due

    async def deliver_reminders():
        push = store.get("push:feishu")
        if not push:
            return
        for reminder in due_reminders():
            if (reminder.get("item_id") or reminder.get("kind") in ("item","digest","class")) and not reminder.get("notify_feishu",False):
                continue
            with store.connect() as db:
                db.execute("INSERT OR IGNORE INTO deliveries (reminder_id,channel) VALUES (?,?)", (reminder["id"], "feishu"))
                record = db.execute("SELECT * FROM deliveries WHERE reminder_id=? AND channel='feishu'", (reminder["id"],)).fetchone()
            if record["delivered"] or record["attempts"] >= 5 or record["next_try"] > time.time():
                continue
            try:
                await push_feishu(push, "[Relay 中转站] " + reminder["title"])
                with store.connect() as db:
                    db.execute("UPDATE deliveries SET delivered=1 WHERE reminder_id=? AND channel='feishu'", (reminder["id"],))
            except Exception:
                with store.connect() as db:
                    db.execute("UPDATE deliveries SET attempts=attempts+1,next_try=? WHERE reminder_id=? AND channel='feishu'", (
                        time.time()+min(3600, 30*2**record["attempts"]), reminder["id"]))

    async def worker():
        next_sync = 0
        while True:
            try:
                if time.time() >= next_sync:
                    await sync_sources()
                    next_sync = time.time() + 120
                if settings()["auto_plan"]:
                    proposal = organizer.plan()
                    if proposal["tasks"]:
                        save_tasks(proposal["tasks"])
                await deliver_reminders()
                organizer.digest()
                with store.connect() as db:
                    db.execute("DELETE FROM receipts WHERE expires<?", (time.time(),))
                for key in list(sessions):
                    if sessions[key]["expires"] < time.time():
                        sessions.pop(key, None)
                for key in list(pending_oauth):
                    if pending_oauth[key]["expires"] < time.time():
                        pending_oauth.pop(key, None)
            except Exception:
                # Preserve worker liveness without dumping confidential payloads.
                store.set("worker_error", {"at": now().isoformat(), "message": "后台任务失败，将自动重试"})
            await asyncio.sleep(15)

    @asynccontextmanager
    async def lifespan(app):
        task = asyncio.create_task(worker()) if background else None
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task

    app = FastAPI(title="Relay Station", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store = store
    app.state.ingest = ingest
    app.state.save_tasks = save_tasks
    app.state.sync_sources = sync_sources
    app.state.deliver_reminders = deliver_reminders
    app.state.organizer=organizer
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=[parsed_origin.hostname, "localhost", "127.0.0.1"])

    @app.middleware("http")
    async def security(request, call_next):
        if request.method in ("POST", "PATCH", "PUT", "DELETE"):
            total, chunks = 0, []
            async for chunk in request.stream():
                total += len(chunk)
                if total > 256_000:
                    return JSONResponse({"detail": "请求体过大"}, 413)
                chunks.append(chunk)
            request._body = b"".join(chunks)
            if request.url.path.startswith("/api/"):
                if request.headers.get("origin") != origin or request.headers.get("x-relay-request") != "1":
                    return JSONResponse({"detail": "来源校验失败"}, 403)
        response = await call_next(request)
        response.headers.update({"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
            "Referrer-Policy": "no-referrer", "Cache-Control": "no-store",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            "Content-Security-Policy": "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"})
        return response

    def get_session(request: Request):
        sid = request.cookies.get("relay_session", "")
        key = hashlib.sha256(sid.encode()).hexdigest()
        session = sessions.get(key)
        if not session or session["expires"] < time.time():
            raise HTTPException(401, "请先解锁本地工作区")
        if request.method not in ("GET", "HEAD") and not hmac.compare_digest(request.headers.get("x-csrf-token", ""), session["csrf"]):
            raise HTTPException(403, "会话校验失败，请重新登录")
        return key

    def issue_session(response):
        sid = secrets.token_urlsafe(32)
        key = hashlib.sha256(sid.encode()).hexdigest()
        sessions[key] = {"csrf": secrets.token_urlsafe(32), "expires": time.time()+12*3600}
        response.set_cookie("relay_session", sid, httponly=True, secure=secure_cookie, samesite="lax", max_age=12*3600, path="/")
        return {"csrf": sessions[key]["csrf"]}

    @app.get("/api/session")
    def session_status(request: Request):
        try:
            key = get_session(request)
            return {"authenticated": True, "needs_setup": False, "csrf": sessions[key]["csrf"]}
        except HTTPException:
            return {"authenticated": False, "needs_setup": store.get("password") is None}

    @app.post("/api/setup")
    def setup(body: Login, request: Request, response: Response):
        if request.client.host not in ("127.0.0.1", "::1", "testclient"):
            raise HTTPException(403, "远程部署请先通过 RELAY_ADMIN_PASSWORD 设置初始密码")
        with store.lock:
            if store.get("password"):
                raise HTTPException(409, "已初始化，请使用登录")
            salt = secrets.token_hex(16)
            store.set("password", {"salt": salt, "hash": password_hash(body.password, salt)})
        return issue_session(response)

    @app.post("/api/login")
    def login(body: Login, request: Request, response: Response):
        host = request.client.host
        with store.lock:
            attempts = [t for t in login_attempts.get(host, []) if t > time.time()-300]
            if len(attempts) >= 5:
                raise HTTPException(429, "尝试过多，请 5 分钟后重试")
            login_attempts[host] = attempts + [time.time()]
        saved = store.get("password")
        if not saved or not hmac.compare_digest(password_hash(body.password, saved["salt"]), saved["hash"]):
            raise HTTPException(401, "密码不正确")
        login_attempts.pop(host, None)
        return issue_session(response)

    @app.post("/api/logout")
    def logout(response: Response, key=Depends(get_session)):
        sessions.pop(key, None)
        response.delete_cookie("relay_session", path="/")
        return {"ok": True}

    @app.get("/api/state")
    def state(_=Depends(get_session)):
        connectors = []
        for ident, name, mode in [("gmail", "Gmail", "OAuth 2.0 · 只读"), ("qqmail", "QQ 邮箱", "IMAP TLS · 专用授权码"), ("feishu", "飞书", "企业应用 · 加密事件"), ("canvas","Canvas / 学校作业","授权令牌 · 只读接口"), ("wechat", "微信 / 企业微信", "待实现企业微信适配器"), ("qq", "QQ 机器人", "待实现官方机器人适配器")]:
            connected = bool(store.get("connector:"+ident))
            connectors.append({"id": ident, "name": name, "mode": mode,
                "configured": connected, "status": store.get("status:"+ident, {"state": "configured" if connected else "disconnected"}),
                "available": ident in ("gmail", "qqmail", "feishu", "canvas")})
        with store.connect() as db:
            failed = db.execute("SELECT COUNT(*) FROM deliveries WHERE attempts>=5 AND delivered=0").fetchone()[0]
        memo_items=organizer.decorated()
        canvas=store.get("connector:canvas",{})
        return {"messages": store.messages(), "tasks": store.tasks(), "connectors": connectors,
            "items":memo_items,"gaps":organizer.gaps(memo_items),"blocks":store.blocks(),"profile":organizer_profile(store),
            "canvas":{"base_url":canvas.get("base_url",""),"courses":canvas.get("courses",[]),"course_ids":canvas.get("course_ids",[]),
                      "sync_assignments":canvas.get("sync_assignments",True),"sync_calendar":canvas.get("sync_calendar",False)},
            "settings": settings(), "rules": store.get("rules", DEFAULT_RULES), "timezone": timezone_name,
            "push_configured": bool(store.get("push:feishu")), "failed_deliveries": failed,
            "worker_error": store.get("worker_error"),
            "agent_configured": bool(service_value("agent_base_url") and service_value("agent_api_key") and service_value("agent_model")),
            "google_configured": bool(service_value("google_client_id") and service_value("google_client_secret"))}

    @app.get("/api/services")
    def services(_=Depends(get_session)):
        return public_services()

    @app.put("/api/services")
    def update_services(body: ServiceConfig, _=Depends(get_session)):
        value = body.model_dump()
        # Empty secret fields mean keep the existing value, including env fallback.
        for key in ("google_client_secret", "agent_api_key"):
            if not value[key]:
                value[key] = service_value(key)
        store.set("services", value)
        return public_services()

    @app.patch("/api/messages/{ident}")
    def patch_message(ident: str, body: MessagePatch, _=Depends(get_session)):
        changes = body.model_dump(exclude_unset=True, mode="json")
        if changes.get("priority") is not None and changes["priority"] not in PRIORITIES:
            raise HTTPException(422, "未知优先级")
        if changes.get("category") is not None and changes["category"] not in CATEGORIES:
            raise HTTPException(422, "未知分类")
        if body.deadline and body.deadline.tzinfo is None:
            raise HTTPException(422, "截止时间需要包含时区")
        if any(value is None for field, value in changes.items() if field != "deadline"):
            raise HTTPException(422, "状态不能为空")
        with store.connect() as db:
            row = db.execute("SELECT payload FROM messages WHERE id=?", (ident,)).fetchone()
            if not row:
                raise HTTPException(404, "消息不存在")
            value = dict(store.decrypt(row[0]), **changes)
            if "priority" in changes:
                value["priority_manual"]=True
            if "category" in changes:
                value["category_manual"]=True
            db.execute("UPDATE messages SET payload=? WHERE id=?", (store.encrypt(value), ident))
            if value.get("archived") or value.get("read"):
                db.execute("UPDATE reminders SET acknowledged=1 WHERE id=?", ("msg:"+ident,))
        return value

    @app.post("/api/sync")
    async def sync(_=Depends(get_session)):
        return await sync_sources()

    @app.put("/api/settings")
    def update_settings(body: Settings, _=Depends(get_session)):
        store.set("settings", body.model_dump())
        return body

    @app.put("/api/rules")
    def update_rules(body: list[Rule], _=Depends(get_session)):
        if len(body) > 50:
            raise HTTPException(422, "最多 50 条规则")
        store.set("rules", [r.model_dump() for r in body])
        return {"ok": True}

    @app.post("/api/reclassify")
    def reclassify(_=Depends(get_session)):
        with store.connect() as db:
            for message in store.messages():
                suggestion=classify(message["title"],message["body"],store.get("rules",DEFAULT_RULES))
                if message.get("priority_manual"):
                    suggestion.pop("priority",None)
                if message.get("category_manual"):
                    suggestion.pop("category",None)
                message.update(suggestion)
                db.execute("UPDATE messages SET payload=? WHERE id=?", (store.encrypt(message), message["id"]))
        return {"ok": True}

    @app.post("/api/plan")
    def plan(_=Depends(get_session)):
        proposal = propose(store.messages(), store.tasks()+[b for b in store.blocks() if b["enabled"]], settings(), timezone_name)
        ident = secrets.token_urlsafe(24)
        store.set("plan:"+ident, {**proposal, "expires": time.time()+600})
        return {"id": ident, **proposal}

    @app.post("/api/plan/{ident}/commit")
    def commit(ident: str, _=Depends(get_session)):
        with store.lock:
            proposal = store.get("plan:"+ident)
            if not proposal or proposal["expires"] < time.time():
                raise HTTPException(409, "计划已过期，请重新生成")
            if any(datetime.fromisoformat(t["start"]) <= now() for t in proposal["tasks"]):
                raise HTTPException(409, "计划的开始时间已过去，请重新生成")
            result = save_tasks(proposal["tasks"])
            store.delete("plan:"+ident)
        return {"tasks": result}

    @app.post("/api/tasks")
    def add_task(body: TaskInput, _=Depends(get_session)):
        return save_tasks([body.model_dump(mode="json")])[0]

    @app.patch("/api/tasks/{ident}")
    async def complete_task(ident: str, request: Request, _=Depends(get_session)):
        body = await request.json()
        if set(body) != {"done"} or not isinstance(body["done"], bool):
            raise HTTPException(422, "只接受 done 布尔状态")
        with store.connect() as db:
            if not db.execute("SELECT 1 FROM tasks WHERE id=?", (ident,)).fetchone():
                raise HTTPException(404, "日程不存在")
            db.execute("UPDATE tasks SET done=? WHERE id=?", (int(body["done"]), ident))
            db.execute("UPDATE reminders SET acknowledged=? WHERE task_id=?", (int(body["done"]), ident))
        return {"ok": True}

    @app.delete("/api/tasks/{ident}")
    def delete_task(ident: str, _=Depends(get_session)):
        with store.connect() as db:
            db.execute("DELETE FROM deliveries WHERE reminder_id IN (SELECT id FROM reminders WHERE task_id=?)", (ident,))
            db.execute("DELETE FROM reminders WHERE task_id=?", (ident,))
            db.execute("DELETE FROM tasks WHERE id=?", (ident,))
        return {"ok": True}

    @app.get("/api/reminders")
    def reminders(_=Depends(get_session)):
        return due_reminders()

    @app.post("/api/reminders/{ident}/ack")
    def acknowledge(ident: str, _=Depends(get_session)):
        with store.connect() as db:
            db.execute("UPDATE reminders SET acknowledged=1 WHERE id=?", (ident,))
        return {"ok": True}

    @app.post("/api/connectors/qqmail")
    async def connect_qqmail(body: QQConfig, _=Depends(get_session)):
        if sync_lock.locked():
            raise HTTPException(409, "正在同步，请稍后再试")
        async with sync_lock:
            try:
                messages, cursor = await asyncio.to_thread(qq_sync, body.model_dump())
            except Exception:
                raise HTTPException(400, "邮箱授权失败，请检查专用授权码、IMAP 开关和网络") from None
            store.set("connector:qqmail", body.model_dump())
            for message in messages:
                ingest("qqmail", message)
            store.set("cursor:qqmail", cursor)
            set_status("qqmail", "connected")
        return {"ok": True}

    @app.post("/api/connectors/feishu")
    def connect_feishu(body: FeishuConfig, _=Depends(get_session)):
        store.set("connector:feishu", body.model_dump())
        set_status("feishu", "configured")
        return {"callback_url": origin + "/webhooks/feishu"}

    @app.post("/api/push/feishu")
    def connect_push(body: PushConfig, _=Depends(get_session)):
        store.set("push:feishu", body.model_dump())
        return {"ok": True}

    @app.delete("/api/push/feishu")
    def disconnect_push(_=Depends(get_session)):
        store.delete("push:feishu")
        return {"ok": True}

    @app.delete("/api/connectors/{source}")
    async def disconnect(source: str, _=Depends(get_session)):
        if source not in ("gmail", "qqmail", "feishu", "canvas"):
            raise HTTPException(404, "未知渠道")
        if sync_lock.locked():
            raise HTTPException(409, "正在同步，请稍后再试")
        async with sync_lock:
            store.delete("connector:"+source)
            store.delete("status:"+source)
            store.delete("cursor:"+source)
        return {"ok": True, "note": "本地凭据已删除。请同时在平台管理台撤销授权；历史消息保留。"}

    @app.post("/api/connectors/gmail/authorize")
    def authorize(key=Depends(get_session)):
        client_id = service_value("google_client_id")
        if not client_id or not service_value("google_client_secret"):
            raise HTTPException(400, "请先在偏好设置的服务配置中填写 Google OAuth 客户端信息")
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        pending_oauth[state] = {"verifier": verifier, "session": key, "expires": time.time()+600}
        params = {"client_id": client_id, "redirect_uri": origin+"/oauth/google/callback", "response_type": "code",
                  "scope": "https://www.googleapis.com/auth/gmail.readonly", "access_type": "offline", "prompt": "consent",
                  "state": state, "code_challenge": challenge, "code_challenge_method": "S256"}
        return {"url": "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode(params)}

    @app.get("/oauth/google/callback")
    async def oauth_callback(request: Request):
        key = get_session(request)
        state = request.query_params.get("state", "")
        pending = pending_oauth.pop(state, None)
        if not pending or pending["expires"] < time.time() or pending["session"] != key:
            raise HTTPException(400, "授权状态无效或已过期，请重新发起")
        if request.query_params.get("error") or not request.query_params.get("code"):
            return RedirectResponse("/?oauth=cancelled", status_code=303)
        try:
            async with httpx.AsyncClient(timeout=25) as client:
                response = await client.post("https://oauth2.googleapis.com/token", data={
                    "client_id": service_value("google_client_id"), "client_secret": service_value("google_client_secret"),
                    "code": request.query_params["code"], "redirect_uri": origin+"/oauth/google/callback",
                    "grant_type": "authorization_code", "code_verifier": pending["verifier"]})
                response.raise_for_status()
                token = response.json()
                if not token.get("refresh_token"):
                    raise ValueError("Missing refresh token")
                profile = await client.get("https://gmail.googleapis.com/gmail/v1/users/me/profile",
                    headers={"Authorization": "Bearer " + token["access_token"]})
                profile.raise_for_status()
                token["mailbox_id"] = profile.json()["emailAddress"]
                token["expires_at"] = time.time()+token.get("expires_in", 3600)
                async with sync_lock:
                    previous = store.get("connector:gmail", {})
                    if previous.get("mailbox_id") != token["mailbox_id"]:
                        store.delete("cursor:gmail")
                    store.set("connector:gmail", token)
                    set_status("gmail", "configured")
        except Exception:
            raise HTTPException(400, "授权换取令牌失败，请检查网络与 OAuth 应用配置") from None
        return RedirectResponse("/?oauth=success", status_code=303)

    @app.post("/webhooks/feishu")
    async def feishu_event(request: Request):
        config = store.get("connector:feishu")
        if not config:
            raise HTTPException(404, "事件接入未配置")
        raw = await request.body()
        try:
            event = verify_feishu(raw, request.headers, config)
        except Exception:
            raise HTTPException(403, "事件验证失败") from None
        if event.get("type") == "url_verification":
            return {"challenge": event["challenge"]}
        if not organizer_profile(store)["sources"]["feishu"]["enabled"]:
            return {"ok":True,"paused":True}
        header = event.get("header", {})
        event_id = header.get("event_id")
        if not event_id:
            raise HTTPException(400, "缺少事件 ID")
        with store.connect() as db:
            if db.execute("SELECT 1 FROM receipts WHERE id=?", (event_id,)).fetchone():
                return {"ok": True, "duplicate": True}
            if header.get("event_type") == "im.message.receive_v1":
                message = event.get("event", {}).get("message", {})
                if message.get("message_type") == "text":
                    try:
                        content = json.loads(message.get("content", "{}"))
                        text = content.get("text", "")
                    except (ValueError, TypeError):
                        raise HTTPException(400, "文本消息格式错误") from None
                    sender = event.get("event", {}).get("sender", {}).get("sender_id", {}).get("open_id", "飞书用户")
                    ingest("feishu", {"external_id": message.get("message_id", event_id), "title": text[:100] or "飞书消息", "body": text, "sender": sender})
            db.execute("INSERT OR IGNORE INTO receipts VALUES (?,?)", (event_id, time.time()+86400))
        set_status("feishu", "connected")
        return {"ok": True}

    @app.post("/api/messages/{ident}/analyze")
    async def analyze(ident: str, _=Depends(get_session)):
        if not settings()["agent_enabled"]:
            raise HTTPException(403, "请先在设置中允许所选消息发送给 Agent 服务")
        base_url, api_key, model = (service_value(k) for k in ("agent_base_url", "agent_api_key", "agent_model"))
        if not all((base_url, api_key, model)):
            raise HTTPException(400, "请先在服务端配置 Agent 地址、密钥和模型名称")
        message = next((m for m in store.messages() if m["id"] == ident), None)
        if not message:
            raise HTTPException(404, "消息不存在")
        try:
            result = await agent_analyze(message, base_url, api_key, model)
        except Exception:
            raise HTTPException(502, "Agent 调用失败或返回格式不正确，本地分类仍可使用") from None
        # Return a suggestion only. Client must explicitly apply the proposed category/priority.
        return result

    @app.get("/health")
    def health():
        return {"ok": True, "version": __version__}

    @app.get("/")
    def index():
        return FileResponse(ROOT / "static" / "index.html")

    register_organizer(app,organizer,get_session,save_tasks,sync_lock,sync_sources)
    app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
    return app
