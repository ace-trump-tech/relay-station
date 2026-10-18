"""Deterministic classification and conflict-free schedule proposals."""
import re
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

DEFAULTS = {
    "work_start": "09:00", "work_end": "18:00", "lunch_start": "12:00",
    "lunch_end": "13:00", "weekdays": [0, 1, 2, 3, 4], "duration": 30,
    "remind_minutes": 10, "quiet_start": "22:00", "quiet_end": "08:00",
    "agent_enabled": False, "auto_plan": False, "desktop_notifications": False,
}
DEFAULT_RULES = [
    {"id": "urgent", "name": "紧急事项优先", "keywords": "紧急,尽快,urgent,阻塞,截止,今天内", "priority": "high", "category": "工作", "enabled": True},
    {"id": "meeting", "name": "会议与协作", "keywords": "会议,评审,meeting,同步,周会", "priority": "normal", "category": "会议", "enabled": True},
    {"id": "bill", "name": "账单与财务", "keywords": "账单,发票,报销,invoice,付款", "priority": "normal", "category": "财务", "enabled": True},
    {"id": "news", "name": "订阅安静收纳", "keywords": "newsletter,退订,订阅,周刊", "priority": "low", "category": "订阅", "enabled": True},
]


def classify(title, body, rules):
    text = (title + " " + body).lower()
    for rule in rules:
        if rule.get("enabled") and any(k.strip().lower() in text for k in rule["keywords"].split(",") if k.strip()):
            return {"priority": rule["priority"], "category": rule["category"], "reason": "匹配规则：" + rule["name"]}
    return {"priority": "normal", "category": "工作", "reason": "默认分类，可手动调整"}


def quiet_now(settings, now):
    start, end = settings["quiet_start"], settings["quiet_end"]
    current = now.strftime("%H:%M")
    if start == end:
        return False
    return start <= current < end if start < end else current >= start or current < end


def propose(messages, existing, settings, timezone, now=None):
    tz = ZoneInfo(timezone)
    now = now or datetime.now(tz)
    now = now.astimezone(tz)
    busy = [(datetime.fromisoformat(t["start"]), datetime.fromisoformat(t["end"]))
            for t in existing if not t.get("done")]
    planned = {t.get("message_id") for t in existing}
    planned_items = {t.get("item_id") for t in existing}
    proposals, unscheduled = [], []
    candidates = [m for m in messages if m["id"] not in (planned_items if m.get("_item") else planned) and not m.get("archived")
                  and m.get("category") != "订阅" and m.get("priority") != "low"]
    candidates.sort(key=lambda m: (m.get("priority") != "high", m.get("deadline") or "9999"))
    for message in candidates[:30]:
        duration = timedelta(minutes=message.get("duration",settings["duration"]))
        selected = None
        deadline = datetime.fromisoformat(message["deadline"]) if message.get("deadline") else None
        for day_offset in range(14):
            day = now.date() + timedelta(days=day_offset)
            if day.weekday() not in settings["weekdays"]:
                continue
            def at(value):
                return datetime.combine(day, time.fromisoformat(value), tz)
            start = max(at(settings["work_start"]), now + timedelta(minutes=1))
            # Round to the next five-minute boundary.
            start = start.replace(second=0, microsecond=0) + timedelta(minutes=(5-start.minute % 5) % 5)
            while start + duration <= at(settings["work_end"]):
                end = start + duration
                lunch = (at(settings["lunch_start"]), at(settings["lunch_end"]))
                if deadline and end > deadline:
                    break
                if not any(start < b and end > a for a, b in busy + [lunch]):
                    selected = (start, end)
                    break
                start += timedelta(minutes=5)
            if selected:
                break
        if not selected:
            unscheduled.append({"message_id": message["id"], "title": message["title"], "reason": "未来 14 天内没有满足截止时间的空档"})
            continue
        start, end = selected
        busy.append(selected)
        proposals.append({"message_id": None if message.get("_item") else message["id"],
                          **({"item_id":message["id"]} if message.get("_item") else {}), "title": message["title"][:200],
                          "start": start.isoformat(), "end": end.isoformat(),
                          "priority": message["priority"], "source": message["source"]})
    return {"tasks": proposals, "unscheduled": unscheduled}
