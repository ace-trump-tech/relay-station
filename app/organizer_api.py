"""Authenticated organizer routes, kept separate from the platform adapters."""
import csv
import io
import secrets
import time
from datetime import datetime, timedelta
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from .canvas import Canvas, canvas_origin
from .organizer import IMPORTANCE, PROFILE, STATUSES, Organizer, iso, profile, stamp


class Factors(BaseModel):
    relevance: float=Field(default=.5,ge=0,le=1)
    loss: float=Field(default=.5,ge=0,le=1)
    commitment: float=Field(default=.5,ge=0,le=1)


Importance=Literal["critical","high","normal","low"]
Status=Literal["inbox","todo","doing","waiting","done","dismissed"]


class ItemInput(BaseModel):
    title: str=Field(min_length=1,max_length=200)
    notes: str=Field(default="",max_length=20000)
    topic: str=Field(default="工作",min_length=1,max_length=40)
    course: str=Field(default="",max_length=200)
    due_at: datetime|None=None
    importance_override: Importance|None=None
    status: Status="todo"
    verification: Literal["confirmed","needs_review"]="confirmed"
    factors: Factors=Field(default_factory=Factors)
    estimated_minutes: int=Field(default=30,ge=5,le=480)
    buffer_minutes: int=Field(default=60,ge=0,le=10080)
    include_in_plan: bool=False
    remind_mode: Literal["off","digest","deadline"]="deadline"
    remind_offsets: list[int]=Field(default_factory=lambda:[1440,60],max_length=8)
    notify_feishu: bool=False
    source_url: str=Field(default="",max_length=2000)

    @field_validator("due_at")
    @classmethod
    def aware(cls,value):
        if value and value.tzinfo is None:
            raise ValueError("截止时间需要包含时区")
        return value

    @field_validator("remind_offsets")
    @classmethod
    def offsets(cls,value):
        if any(n<0 or n>43200 for n in value):
            raise ValueError("提醒提前量必须在 0 至 43200 分钟内")
        return sorted(set(value),reverse=True)

    @field_validator("source_url")
    @classmethod
    def safe_link(cls,value):
        if value:
            from urllib.parse import urlsplit
            parsed=urlsplit(value)
            if parsed.scheme!="https" or not parsed.hostname or parsed.username or parsed.password:
                raise ValueError("来源链接仅允许 HTTPS 地址")
        return value


class ItemPatch(BaseModel):
    title: str|None=None
    notes: str|None=None
    topic: str|None=None
    course: str|None=None
    due_at: datetime|None=None
    importance_override: Importance|None=None
    status: Status|None=None
    verification: Literal["confirmed","needs_review"]|None=None
    factors: Factors|None=None
    estimated_minutes: int|None=None
    buffer_minutes: int|None=None
    include_in_plan: bool|None=None
    remind_mode: Literal["off","digest","deadline"]|None=None
    remind_offsets: list[int]|None=None
    notify_feishu: bool|None=None
    source_url: str|None=None


class SourcePolicy(BaseModel):
    enabled: bool=True
    capture: bool=True


class CaptureRule(BaseModel):
    id: str=Field(min_length=1,max_length=64)
    name: str=Field(min_length=1,max_length=80)
    source: Literal["all","gmail","qqmail","feishu","canvas"]="all"
    sender: str=Field(default="",max_length=200)
    keywords: str=Field(min_length=1,max_length=1000)
    topic: str=Field(default="工作",min_length=1,max_length=40)
    importance: Importance|None=None
    enabled: bool=True


class ProfileInput(BaseModel):
    personas: list[str]=Field(default_factory=lambda:["学生"],max_length=8)
    goals: str=Field(default="",max_length=2000)
    weights: dict[str,int]=Field(default_factory=lambda:dict(PROFILE["weights"]))
    notify_messages: bool=False
    daily_digest: bool=True
    digest_time: str=Field(default="08:00",pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    default_remind_offsets: list[int]=Field(default_factory=lambda:[1440,60],max_length=8)
    stale_days: int=Field(default=3,ge=1,le=60)
    sources: dict[str,SourcePolicy]=Field(default_factory=lambda:{s:SourcePolicy() for s in PROFILE["sources"]})
    capture_rules: list[CaptureRule]=Field(default_factory=list,max_length=50)

    @model_validator(mode="after")
    def validate_profile(self):
        if set(self.weights)!={"relevance","loss","commitment"} or any(n<0 or n>100 for n in self.weights.values()) or sum(self.weights.values())==0:
            raise ValueError("请设置相关性、遗漏损失、本人责任三个非负权重，且至少一项大于 0")
        if set(self.sources)-set(PROFILE["sources"]):
            raise ValueError("未知来源")
        if any(len(s)>40 for s in self.personas) or any(n<0 or n>43200 for n in self.default_remind_offsets):
            raise ValueError("角色或提醒提前量不合法")
        return self


class BlockInput(BaseModel):
    title: str=Field(min_length=1,max_length=200)
    start: datetime
    end: datetime
    location: str=Field(default="",max_length=200)
    weeks: int=Field(default=1,ge=1,le=30)
    remind_minutes: int|None=Field(default=10,ge=0,le=1440)

    @model_validator(mode="after")
    def times(self):
        if self.start.tzinfo is None or self.end.tzinfo is None or not self.start<self.end or self.end-self.start>timedelta(days=1):
            raise ValueError("课程时间须包含时区，结束晚于开始且时长不超过一天")
        return self


class CanvasConfig(BaseModel):
    base_url: str=Field(max_length=1000)
    access_token: str=Field(default="",max_length=4096)
    sync_assignments: bool=True
    sync_calendar: bool=False
    course_ids: list[int]=Field(default_factory=list,max_length=25)

    @field_validator("base_url")
    @classmethod
    def origin(cls,value):
        return canvas_origin(value)

    @field_validator("course_ids")
    @classmethod
    def ids(cls,value):
        if any(n<=0 for n in value):
            raise ValueError("课程 ID 必须大于 0")
        return list(dict.fromkeys(value))


class CSVInput(BaseModel):
    csv: str=Field(min_length=1,max_length=200000)


def register(app,organizer:Organizer,get_session,save_tasks,sync_lock,sync_sources):
    store=organizer.store
    router=APIRouter(dependencies=[Depends(get_session)])

    def item_by_id(ident):
        item=next((i for i in store.items() if i["id"]==ident),None)
        if not item:
            raise HTTPException(404,"事项不存在")
        return item

    @router.get("/api/organizer")
    def state():
        items=organizer.decorated()
        canvas=store.get("connector:canvas",{})
        return {"items":items,"gaps":organizer.gaps(items),"blocks":store.blocks(),"profile":profile(store),
                "canvas":{"base_url":canvas.get("base_url",""),"courses":canvas.get("courses",[]),"course_ids":canvas.get("course_ids",[]),
                          "sync_assignments":canvas.get("sync_assignments",True),"sync_calendar":canvas.get("sync_calendar",False)}}

    @router.put("/api/organizer/profile")
    def set_profile(body:ProfileInput):
        store.set("organizer_profile",body.model_dump())
        return profile(store)

    @router.post("/api/items")
    def create_item(body:ItemInput):
        data=body.model_dump(mode="json")
        data["due_at"]=iso(data["due_at"])
        return organizer.create(data)[0]

    @router.patch("/api/items/{ident}")
    def patch_item(ident:str,body:ItemPatch):
        with store.lock:
            item=item_by_id(ident)
            patch=body.model_dump(exclude_unset=True,mode="json")
            try:
                validated=ItemInput.model_validate({**item,**patch}).model_dump(mode="json")
            except ValidationError:
                raise HTTPException(422,"请检查事项标题、时间、重要程度和提醒选项") from None
            validated["due_at"]=iso(validated["due_at"])
            reminder_fields={"due_at","status","verification","remind_mode","remind_offsets"}
            if any(item.get(k)!=validated.get(k) for k in reminder_fields):
                item["reminder_revision"]=item.get("reminder_revision",0)+1
            item.update(validated)
            return organizer.save(item)

    @router.delete("/api/items/{ident}")
    def delete_item(ident:str):
        item_by_id(ident)
        with store.connect() as db:
            db.execute("UPDATE reminders SET acknowledged=1 WHERE id LIKE ?",("item:"+ident+":%",))
            db.execute("DELETE FROM items WHERE id=?",(ident,))
        return {"ok":True,"note":"已删除本地事项；启用的外部来源后续同步可能重新导入"}

    @router.post("/api/messages/{ident}/capture")
    def capture_one(ident:str):
        message=next((m for m in store.messages() if m["id"]==ident),None)
        if not message:
            raise HTTPException(404,"消息不存在")
        return organizer.capture(message,force=True)

    @router.post("/api/organizer/capture")
    def capture_existing():
        before=len(store.items())
        for message in store.messages():
            organizer.capture(message)
        return {"created":len(store.items())-before}

    @router.post("/api/items/{ident}/merge/{target_id}")
    def merge(ident:str,target_id:str):
        if ident==target_id:
            raise HTTPException(422,"不能合并到自己")
        with store.lock:
            source,target=item_by_id(ident),item_by_id(target_id)
            target["sources"]=(target.get("sources",[])+source.get("sources",[]))[-30:]
            target["notes"]=(target["notes"]+"\n\n合并记录："+source["title"]+"\n"+source["notes"])[:20000]
            if target.get("due_at")!=source.get("due_at"):
                target["verification"]="needs_review"
                target["changes"]=(target.get("changes",[])+[{"at":stamp(),"note":"合并项截止时间不同，请核实"}])[-10:]
            organizer.save(target)
            organizer.save({**source,"status":"dismissed","merged_into":target_id})
            return target

    @router.post("/api/items/plan")
    def plan_items():
        proposal=organizer.plan()
        ident=secrets.token_urlsafe(24)
        store.set("plan:"+ident,{**proposal,"expires":time.time()+600})
        return {"id":ident,**proposal}

    @router.post("/api/blocks")
    def add_block(body:BlockInput):
        blocks=[]
        for week in range(body.weeks):
            blocks.append(organizer.upsert_block({"title":body.title,"start":iso(body.start+timedelta(weeks=week)),
                "end":iso(body.end+timedelta(weeks=week)),"location":body.location,"remind_minutes":body.remind_minutes}))
        return {"blocks":blocks}

    @router.post("/api/blocks/import")
    def import_blocks(body:CSVInput):
        try:
            rows=list(csv.DictReader(io.StringIO(body.csv.lstrip("\ufeff")))); parsed=[]
            if not rows or len(rows)>200:
                raise ValueError("empty or large CSV")
            for row in rows:
                parsed.append(BlockInput(title=row["title"],start=row["start"],end=row["end"],location=row.get("location", "")))
        except (KeyError,ValueError,ValidationError):
            raise HTTPException(422,"CSV 需要 title,start,end 列，时间含时区；每次最多 200 行") from None
        return {"blocks":[organizer.upsert_block({"title":b.title,"start":iso(b.start),"end":iso(b.end),"location":b.location,"remind_minutes":b.remind_minutes}) for b in parsed]}

    @router.patch("/api/blocks/{ident}")
    def toggle_block(ident:str,body:dict):
        if not body or set(body)-{"enabled","remind_minutes"} or ("enabled" in body and not isinstance(body["enabled"],bool)):
            raise HTTPException(422,"只接受占用状态和提前提醒分钟数")
        if "remind_minutes" in body and body["remind_minutes"] is not None and (type(body["remind_minutes"]) is not int or not 0<=body["remind_minutes"]<=1440):
            raise HTTPException(422,"提前提醒范围为 0 至 1440 分钟，null 表示关闭")
        block=next((b for b in store.blocks() if b["id"]==ident),None)
        if not block:
            raise HTTPException(404,"课程安排不存在")
        return organizer.upsert_block({**block,**body},block["external_key"])

    @router.delete("/api/blocks/{ident}")
    def delete_block(ident:str):
        with store.connect() as db:
            db.execute("UPDATE reminders SET acknowledged=1 WHERE id LIKE ?",("class:"+ident+":%",))
            db.execute("DELETE FROM busy_blocks WHERE id=?",(ident,))
        return {"ok":True}

    @router.post("/api/connectors/canvas")
    async def connect_canvas(body:CanvasConfig):
        if sync_lock.locked():
            raise HTTPException(409,"正在同步，请稍后配置")
        async with sync_lock:
            values=body.model_dump()
            if not values["access_token"]:
                previous=store.get("connector:canvas",{})
                if previous.get("base_url")==values["base_url"]:
                    values["access_token"]=previous.get("access_token", "")
            if len(values["access_token"])<8:
                raise HTTPException(422,"请填写学校允许使用的 Canvas 授权令牌")
            try:
                identity=await Canvas(values).verify()
            except Exception:
                raise HTTPException(400,"Canvas 验证失败，请检查学校地址、授权令牌与管理员允许的 API 权限") from None
            store.set("connector:canvas",{**values,**identity})
            store.set("status:canvas",{"state":"configured","last_sync":stamp()})
        return {"ok":True,**identity}

    app.include_router(router)
