"""Personal reminders, explainable importance, and missed-action recovery.

Incoming material is evidence. User decisions are authoritative. The algorithm
suggests a rank, never changes a user's explicit importance or confirms a date.
"""
import hashlib
import re
import uuid
from datetime import datetime, timedelta, time, timezone
from zoneinfo import ZoneInfo

from .engine import DEFAULTS, propose

TOPICS = ["课程", "作业", "科研", "竞赛", "升学", "工作", "生活", "资料"]
IMPORTANCE = {"critical": "必须关注", "high": "重要", "normal": "普通", "low": "低打扰"}
STATUSES = {"inbox": "待确认", "todo": "待办", "doing": "进行中", "waiting": "等待他人", "done": "已完成", "dismissed": "已忽略"}
PROFILE = {"personas": ["学生"], "goals": "", "weights": {"relevance": 45, "loss": 35, "commitment": 20},
           "notify_messages": False, "daily_digest": True, "digest_time": "08:00",
           "default_remind_offsets": [1440, 60], "stale_days": 3,
           "sources": {s: {"enabled": True, "capture": True} for s in ("gmail", "qqmail", "feishu", "canvas")},
           "capture_rules": []}


def profile(store):
    saved = store.get("organizer_profile", {})
    return {**PROFILE, **saved, "sources": {**PROFILE["sources"], **saved.get("sources", {})}}


def stamp():
    return datetime.now(timezone.utc).isoformat()


def iso(value):
    if not value:
        return None
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return result.astimezone(timezone.utc).isoformat()


def available_minutes(start, end, settings, blocks):
    """Working capacity in the next 31 days; union overlaps to avoid double-counting."""
    if end <= start:
        return 0
    end = min(end, start + timedelta(days=31))
    busy = sorted((datetime.fromisoformat(b["start"]), datetime.fromisoformat(b["end"])) for b in blocks
                  if b.get("enabled", True) and not b.get("done"))
    total = 0
    for offset in range((end.date()-start.date()).days+1):
        day = start.date()+timedelta(days=offset)
        if day.weekday() not in settings["weekdays"]:
            continue
        def at(value):
            return datetime.combine(day, time.fromisoformat(value), start.tzinfo)
        a, b = max(start, at(settings["work_start"])), min(end, at(settings["work_end"]))
        if b <= a:
            continue
        spans = [(max(a,x),min(b,y)) for x,y in busy+[(at(settings["lunch_start"]),at(settings["lunch_end"]))]]
        cursor = a
        for x,y in sorted(spans):
            if y <= x:
                continue
            if x > cursor:
                total += (x-cursor).total_seconds()/60
            cursor = max(cursor,y)
        if cursor < b:
            total += (b-cursor).total_seconds()/60
    return round(total)


def assess(item, pref, settings, blocks, tz_name, current=None):
    current = (current or datetime.now(ZoneInfo(tz_name))).astimezone(ZoneInfo(tz_name))
    weights = pref["weights"]
    factors = item.get("factors", {"relevance": .5,"loss": .5,"commitment": .5})
    denominator = sum(weights.values()) or 100
    score = round(100*sum(factors[k]*weights[k] for k in weights)/denominator)
    suggested = "critical" if score >= 90 else "high" if score >= 75 else "normal" if score >= 45 else "low"
    effective = item.get("importance_override") or suggested
    rank_score = {"critical":100,"high":80,"normal":50,"low":20}[effective] if item.get("importance_override") else score
    reasons = [f"目标相关 {round(factors['relevance']*100)}，遗漏损失 {round(factors['loss']*100)}，本人责任 {round(factors['commitment']*100)}"]
    if item.get("importance_override"):
        reasons.append("采用你的手动重要程度，自动分析不会覆盖")
    urgency, slack, capacity = 0, None, None
    if item.get("due_at"):
        due = datetime.fromisoformat(item["due_at"])
        if due <= current:
            urgency = 100
            reasons.append("已过记录中的截止时间，请核对完成情况")
        elif due <= current+timedelta(days=31):
            capacity = available_minutes(current, due, settings, blocks)
            slack = capacity-item["estimated_minutes"]-item["buffer_minutes"]
            urgency = round(100*max(0, min(1, 1-slack/480)))
            reasons.append(f"截止前工作空档约 {capacity} 分钟，准备余量约 {slack} 分钟")
    else:
        reasons.append("截止时间尚未填写")
    if item.get("verification") != "confirmed":
        reasons.append("来源日期或条件待你确认，不自动启动截止提醒")
    return {"score": score, "suggested": suggested, "importance": effective,
            "manual": bool(item.get("importance_override")), "urgency": urgency,
            "attention": round(.65*rank_score+.35*urgency), "slack_minutes": slack,
            "available_minutes": capacity, "reasons": reasons}


def extract_message(message, pref, tz_name):
    text = message["title"]+"\n"+message["body"]
    topic = next((name for name,words in [
        ("作业",r"作业|assignment|homework|problem set"),("升学",r"夏令营|保研|推免|招生|申请材料"),
        ("竞赛",r"竞赛|比赛|contest"),("科研",r"科研|论文|实验|开题"),
        ("课程",r"上课|课表|教室|课程|lecture|classroom"),("工作",r"提交|会议|截止|deadline|评审")]
        if re.search(words,text,re.I)), None)
    rule = next((r for r in pref["capture_rules"] if r.get("enabled",True)
                 and r.get("source","all") in ("all",message["source"])
                 and (not r.get("sender") or r["sender"].lower() in message["sender"].lower())
                 and any(word.strip().lower() in text.lower() for word in r["keywords"].split(",") if word.strip())),None)
    if not topic and not rule:
        return None
    if rule:
        topic = rule.get("topic",topic or "工作")
    dates=[]
    # Only explicit year + month + day + clock are extracted; date-only and
    # relative expressions stay unfilled rather than silently inventing midnight.
    for match in re.finditer(r"(20\d{2})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2})[:：](\d{2})",text):
        try:
            dt=datetime(*map(int,match.groups()),tzinfo=ZoneInfo(tz_name))
            dates.append((dt.astimezone(timezone.utc).isoformat(),match.group(0)))
        except ValueError:
            continue
    unique=list(dict.fromkeys(d[0] for d in dates))
    due=unique[0] if len(unique)==1 else None
    goals=[w.strip().lower() for w in re.split(r"[,，\n]",pref["goals"]) if w.strip()]
    relevant=.9 if any(w in text.lower() for w in goals) else .5
    return {"title":message["title"][:200],"notes":message["body"][:20000],"topic":topic,
        "due_at":due,"verification":"needs_review","status":"inbox",
        "factors":{"relevance":relevant,"loss":.7 if due else .5,"commitment":.6},
        "importance_override":rule.get("importance") if rule else None,
        "estimated_minutes":30,"buffer_minutes":60,"include_in_plan":False,
        "remind_mode":"deadline","remind_offsets":pref["default_remind_offsets"],"notify_feishu":False,
        "evidence":"；".join(d[1] for d in dates)[:1000] or "未识别到唯一且明确的日期时间，请手动确认",
        "source":message["source"],"source_url":"",
        "sources":[{"source":message["source"],"message_id":message["id"],"sender":message["sender"],"title":message["title"]}]}


class Organizer:
    def __init__(self, store, timezone_name, settings):
        self.store,self.timezone,self.settings=store,timezone_name,settings

    def save(self,item):
        item["updated_at"]=stamp()
        with self.store.connect() as db:
            db.execute("INSERT INTO items VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET updated_at=excluded.updated_at,payload=excluded.payload",
                (item["id"],item["dedupe_key"],item["created_at"],item["updated_at"],self.store.encrypt(item)))
            active_ids=set()
            if item["status"] not in ("done","dismissed","inbox") and item["verification"]=="confirmed" and item.get("due_at") and item["remind_mode"]=="deadline":
                for minutes in sorted(set(item["remind_offsets"]+[0]),reverse=True):
                    due=(datetime.fromisoformat(item["due_at"])-timedelta(minutes=minutes)).astimezone(timezone.utc).isoformat()
                    ident="item:"+item["id"]+":"+str(minutes)+":"+str(int(datetime.fromisoformat(item["due_at"]).timestamp()))+":"+str(item.get("reminder_revision",0))
                    active_ids.add(ident)
                    value={"id":ident,"item_id":item["id"],"title":("即将截止：" if minutes==0 else "备忘提醒：")+item["title"],
                           "due":due,"source":item["source"],"kind":"item","notify_feishu":item.get("notify_feishu",False)}
                    # A previously acknowledged reminder must stay acknowledged on
                    # an unrelated edit; only a new deadline creates a fresh identity.
                    existing=db.execute("SELECT acknowledged FROM reminders WHERE id=?",(ident,)).fetchone()
                    if not existing:
                        db.execute("INSERT INTO reminders (id,due,payload) VALUES (?,?,?)",(ident,due,self.store.encrypt(value)))
                    else:
                        db.execute("UPDATE reminders SET payload=? WHERE id=?",(self.store.encrypt(value),ident))
            for row in db.execute("SELECT id FROM reminders WHERE id LIKE ?",("item:"+item["id"]+":%",)).fetchall():
                if row["id"] not in active_ids:
                    db.execute("UPDATE reminders SET acknowledged=1 WHERE id=?",(row["id"],))
            if item["status"] in ("done","dismissed"):
                db.execute("UPDATE tasks SET done=1 WHERE item_id=?",(item["id"],))
                db.execute("UPDATE reminders SET acknowledged=1 WHERE task_id IN (SELECT id FROM tasks WHERE item_id=?)",(item["id"],))
        return item

    def create(self,values,key=None):
        with self.store.lock:
            key=key or "manual:"+uuid.uuid4().hex
            with self.store.connect() as db:
                row=db.execute("SELECT payload FROM items WHERE dedupe_key=?",(key,)).fetchone()
            if row:
                return self.store.decrypt(row[0]),False
            item={"id":uuid.uuid4().hex,"dedupe_key":key,"created_at":stamp(),"sources":[],"source":"manual",
                  "source_url":"","evidence":"用户手动记录","changes":[],**values}
            return self.save(item),True

    def capture(self,message,force=False):
        pref=profile(self.store)
        policy=pref["sources"].get(message["source"],{"enabled":True,"capture":True})
        if not force and not (policy["enabled"] and policy["capture"]):
            return None
        values=extract_message(message,pref,self.timezone)
        if not values:
            if not force:
                return None
            values=extract_message({**message,"title":"待办："+message["title"]}, {**pref,"capture_rules":[{"keywords":"待办","topic":"工作"}]},self.timezone)
        # Cross-channel merge only for exact normalized titles AND an explicit
        # timestamp. Ambiguous messages stay separate and can be merged manually.
        normalized=re.sub(r"\s+","",message["title"]).lower()
        key="message:"+message["id"]
        if values.get("due_at") and len(normalized)>=8:
            key="exact:"+hashlib.sha256((normalized+values["due_at"]).encode()).hexdigest()
        item,created=self.create(values,key)
        if not created and not any(s.get("message_id")==message["id"] for s in item["sources"]):
            item["sources"]=(item["sources"]+values["sources"])[-30:]
            self.save(item)
        return item

    def upsert_block(self,values,key=None):
        key=key or "manual:"+uuid.uuid4().hex
        with self.store.connect() as db:
            row=db.execute("SELECT payload FROM busy_blocks WHERE external_key=?",(key,)).fetchone()
            old=self.store.decrypt(row[0]) if row else {}
            block={**old,"id":old.get("id",uuid.uuid4().hex),"external_key":key,"enabled":old.get("enabled",True),
                   "source":"manual",**values}
            db.execute("INSERT INTO busy_blocks VALUES (?,?,?,?,?) ON CONFLICT(external_key) DO UPDATE SET start=excluded.start,end=excluded.end,payload=excluded.payload",
                (block["id"],key,block["start"],block["end"],self.store.encrypt(block)))
            active=None
            if block["enabled"] and block.get("remind_minutes") is not None:
                due=(datetime.fromisoformat(block["start"])-timedelta(minutes=block["remind_minutes"])).astimezone(timezone.utc).isoformat()
                active=f"class:{block['id']}:{block['start']}:{block['remind_minutes']}"
                reminder={"id":active,"title":"课程 / 固定安排："+block["title"],"due":due,"source":block["source"],"kind":"class","notify_feishu":False}
                db.execute("INSERT OR IGNORE INTO reminders (id,due,payload) VALUES (?,?,?)",(active,due,self.store.encrypt(reminder)))
            for row in db.execute("SELECT id FROM reminders WHERE id LIKE ?",("class:"+block["id"]+":%",)).fetchall():
                if row["id"]!=active:
                    db.execute("UPDATE reminders SET acknowledged=1 WHERE id=?",(row["id"],))
        return block

    def canvas_assignment(self,assignment):
        key="canvas:"+assignment["external_id"]
        with self.store.lock:
            with self.store.connect() as db:
                row=db.execute("SELECT payload FROM items WHERE dedupe_key=?",(key,)).fetchone()
            if row:
                item=self.store.decrypt(row[0])
                previous=item.get("source_snapshot",{})
                snapshot={k:assignment.get(k) for k in ("title","due_at","submission_status")}
                if snapshot==previous:
                    return item
                item["source_snapshot"]=snapshot
                if snapshot.get("submission_status") in ("submitted","graded","pending_review"):
                    item["changes"]=(item.get("changes",[])+[{"at":stamp(),"note":"Canvas 显示已提交，请核对是否可在本地标记完成"}])[-10:]
                if snapshot.get("due_at")!=previous.get("due_at"):
                    item["suggested_due_at"]=snapshot["due_at"]
                    item["verification"]="needs_review"
                    item["changes"]=(item.get("changes",[])+[{"at":stamp(),"note":"Canvas 截止时间变更，保留你的记录，请核对来源更新"}])[-10:]
                return self.save(item)
            if assignment.get("submission_status") in ("submitted","graded","pending_review"):
                return None
            return self.create({"title":assignment["title"],"notes":assignment["notes"],"topic":"作业","source":"canvas",
                "source_url":assignment["source_url"],"course":assignment["course"],"due_at":assignment["due_at"],
                "status":"inbox","verification":"needs_review","importance_override":None,
                "factors":{"relevance":.85,"loss":.75,"commitment":.9},"estimated_minutes":60,"buffer_minutes":60,
                "include_in_plan":False,"remind_mode":"deadline","remind_offsets":profile(self.store)["default_remind_offsets"],
                "notify_feishu":False,"source_snapshot":{k:assignment.get(k) for k in ("title","due_at","submission_status")},
                "evidence":"Canvas 官方 API 返回的作业及截止时间；请确认个人安排"},key)[0]

    def decorated(self):
        pref=profile(self.store)
        blocks=[b for b in self.store.blocks() if b["enabled"]]+self.store.tasks()
        return [{**i,"assessment":assess(i,pref,self.settings(),blocks,self.timezone)} for i in self.store.items()]

    def gaps(self,items=None):
        current=datetime.now(timezone.utc)
        output=[]
        for item in items if items is not None else self.decorated():
            if item["status"] in ("done","dismissed"):
                continue
            reasons=[]
            if item["status"]=="inbox" or item["verification"]!="confirmed":
                reasons.append("尚未确认事项或来源更新")
            if item.get("due_at"):
                remaining=(datetime.fromisoformat(item["due_at"])-current).total_seconds()/3600
                if remaining<=0:
                    reasons.append("已过记录中的截止时间，尚未标记完成")
                elif remaining<=72:
                    reasons.append("未来 72 小时截止")
            if item["assessment"].get("slack_minutes") is not None and item["assessment"]["slack_minutes"]<0:
                reasons.append("可用时间不足以覆盖预计准备时间")
            age=(current-datetime.fromisoformat(item["updated_at"])).days
            if age>=profile(self.store)["stale_days"]:
                reasons.append("连续多日没有更新，需要回顾")
            if reasons:
                output.append({"item_id":item["id"],"title":item["title"],"reasons":reasons,"importance":item["assessment"]["importance"]})
        return output

    def plan(self):
        candidates=[]
        for item in self.decorated():
            if item["status"] not in ("todo","doing") or item["verification"]!="confirmed" or not item["include_in_plan"]:
                continue
            due=(datetime.fromisoformat(item["due_at"])-timedelta(minutes=item["buffer_minutes"])).isoformat() if item.get("due_at") else None
            candidates.append({"id":item["id"],"title":item["title"],"source":item["source"],"category":item["topic"],
                "priority":"high" if item["assessment"]["importance"] in ("critical","high") else "normal",
                "deadline":due,"duration":item["estimated_minutes"],"_item":True})
        busy=self.store.tasks()+[b for b in self.store.blocks() if b["enabled"]]
        return propose(candidates,busy,self.settings(),self.timezone)

    def digest(self):
        pref=profile(self.store)
        current=datetime.now(ZoneInfo(self.timezone))
        day=current.date().isoformat()
        if not pref["daily_digest"] or current.strftime("%H:%M")<pref["digest_time"] or self.store.get("last_item_digest")==day:
            return
        items=self.decorated()
        gaps=self.gaps(items)
        active=[i for i in items if i["status"] not in ("done","dismissed") and i["remind_mode"]!="off"]
        if active:
            ident="digest:"+day
            reminder={"id":ident,"title":f"今日备忘：{len(active)} 项待跟进，{len(gaps)} 项建议回顾", "source":"manual",
                      "kind":"digest","due":stamp(),"notify_feishu":False}
            with self.store.connect() as db:
                db.execute("INSERT OR IGNORE INTO reminders (id,due,payload) VALUES (?,?,?)",(ident,reminder["due"],self.store.encrypt(reminder)))
        self.store.set("last_item_digest",day)
