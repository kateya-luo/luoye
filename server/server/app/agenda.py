"""M3 日历/议程支柱：事件、待办、提醒及 MARK 语音待办。"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from .auth import CurrentUser, require_auth
from .deepseek_client import DeepSeekClient

DEFAULT_TZ = "Asia/Shanghai"
VALID_TYPES = {"meeting", "class", "todo", "reminder"}
VALID_SOURCES = {"voice", "manual", "meeting"}
_llm = DeepSeekClient()


def get_timezone(name: str):
    # Windows 精简 Python 可能没有 IANA tzdata；上海自 1991 年后无夏令时，可安全固定 UTC+8。
    if name == "Asia/Shanghai":
        return timezone(timedelta(hours=8), name)
    return ZoneInfo(name)


def _id() -> str:
    return uuid.uuid4().hex


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def parse_datetime(value: Any, tz_name: str = DEFAULT_TZ) -> datetime:
    tz = get_timezone(tz_name)
    if isinstance(value, (int, float)):
        seconds = value / 1000 if value > 10_000_000_000 else value
        return datetime.fromtimestamp(seconds, timezone.utc)
    text = str(value or "").strip()
    if not text:
        raise ValueError("缺少时间")
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    return parsed.replace(tzinfo=tz) if parsed.tzinfo is None else parsed


def extract_voice_todo(text: str, now: datetime | None = None,
                       tz_name: str = DEFAULT_TZ) -> dict[str, Any]:
    """无外部模型也可回归的中英文口语时间解析；后续可替换为 LLM extractor。"""
    tz = get_timezone(tz_name)
    now = (now or datetime.now(tz)).astimezone(tz)
    raw = " ".join((text or "").strip().split())
    if not raw:
        raise ValueError("没有可提取的语音文本")
    day = now.date() + timedelta(days=1 if re.search(r"明天|tomorrow", raw, re.I) else 0)
    hour = minute = None
    m = re.search(r"\b(\d{1,2})(?::|：)(\d{2})\b", raw)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
    if hour is None:
        m = re.search(r"\b(\d{1,2})(?:\s*)(am|pm)\b", raw, re.I)
        if m:
            hour, minute = int(m.group(1)) % 12, 0
            if m.group(2).lower() == "pm":
                hour += 12
    if hour is None:
        cn = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
              "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}
        m = re.search(r"(凌晨|早上|上午|中午|下午|晚上)?([一二三四五六七八九十]{1,2}|\d{1,2})点(?:(半)|([一二三四五六七八九十]{1,2}|\d{1,2})分?)?", raw)
        if m:
            hour = cn.get(m.group(2), int(m.group(2)) if m.group(2).isdigit() else 0)
            minute = 30 if m.group(3) else (cn.get(m.group(4), int(m.group(4)) if m.group(4) and m.group(4).isdigit() else 0) if m.group(4) else 0)
            if m.group(1) in {"下午", "晚上"} and hour < 12:
                hour += 12
    if hour is None:
        raise ValueError("没有识别到明确时间，请说例如“晚上七点开会”")
    if not 0 <= hour <= 23 or not 0 <= minute <= 59:
        raise ValueError("识别到的时间无效")
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=tz)
    if day == now.date() and start <= now:
        start += timedelta(days=1)
    title = re.sub(r"明天|今天|今晚|tomorrow|today|tonight", "", raw, flags=re.I)
    title = re.sub(r"(凌晨|早上|上午|中午|下午|晚上)?([一二三四五六七八九十]{1,2}|\d{1,2})点(?:半|[一二三四五六七八九十]{1,2}分?)?", "", title)
    title = re.sub(r"\b\d{1,2}(?::\d{2}|\s*(?:am|pm))\b", "", title, flags=re.I)
    title = re.sub(r"^(提醒我|记一下|待办|please remind me to)\s*", "", title, flags=re.I).strip(" ，,。.")
    return {"title": title or "语音待办", "start": start,
            "type": "meeting" if re.search(r"开会|会议|meeting", raw, re.I) else "todo"}


def clean_voice_todo_content(text: str) -> str:
    """Remove conversational wrappers while preserving the actual action.

    This is deliberately conservative: it strips common recorder commands but
    never asks the fallback parser to invent an action that was not spoken.
    """
    value = " ".join((text or "").strip().split())
    value = re.sub(
        r"^(?:有个事情|有件事情|有一件事|这件事情|这个事情)?"
        r"(?:你)?(?:帮我)?(?:先)?(?:记一下|记下来|记录一下|记住|提醒我|提醒一下|创建(?:一个)?待办|加(?:一个)?待办)"
        r"[，,：:\s]*",
        "", value, flags=re.I)
    value = re.sub(
        r"^(?:到时候|之后|以后)?(?:我)?(?:要做|需要做|要|需要|得|应该)?(?:记得|别忘了)?"
        r"[，,：:\s]*", "", value, flags=re.I)
    value = re.sub(r"^(?:我要|要做|需要做|待办是|事情是)[，,：:\s]*", "", value,
                   flags=re.I)
    value = re.sub(r"[，,。.!！?？\s]+$", "", value)
    return value or "语音待办"


def extract_optional_voice_todo(text: str, now: datetime | None = None,
                                tz_name: str = DEFAULT_TZ) -> dict[str, Any]:
    """Best-effort fallback for a todo whose due time is optional.

    Exact clock expressions continue to use ``extract_voice_todo``.  A spoken
    day period without a clock uses a documented, predictable default.  Words
    such as "之后" and "有空" are not treated as deadlines.
    """
    tz = get_timezone(tz_name)
    now = (now or datetime.now(tz)).astimezone(tz)
    raw = " ".join((text or "").strip().split())
    if not raw:
        raise ValueError("没有可提取的语音文本")
    try:
        return extract_voice_todo(raw, now, tz_name)
    except ValueError:
        pass

    due: datetime | None = None
    day_offset = 1 if re.search(r"明天|tomorrow", raw, re.I) else 0
    period = re.search(r"凌晨|早上|上午|中午|下午|晚上|今晚", raw)
    if period:
        default_hour = {
            "凌晨": 6, "早上": 8, "上午": 9, "中午": 12,
            "下午": 15, "晚上": 19, "今晚": 19,
        }[period.group(0)]
        day = now.date() + timedelta(days=day_offset)
        due = datetime(day.year, day.month, day.day, default_hour, 0, tzinfo=tz)
        if day_offset == 0 and due <= now:
            due += timedelta(days=1)

    title = re.sub(r"明天|今天|今晚|tomorrow|today|tonight", "", raw, flags=re.I)
    title = re.sub(r"凌晨|早上|上午|中午|下午|晚上", "", title)
    title = clean_voice_todo_content(title)
    return {
        "title": title,
        "start": due,
        "type": "meeting" if re.search(r"开会|会议|meeting", raw, re.I) else "todo",
    }


class EventInput(BaseModel):
    owner: str = "default"
    type: str = "todo"
    title: str
    start: str | int | float
    end: str | int | float | None = None
    recurrence_rule: str | None = None
    source: str = "manual"
    linked_meeting_id: str | None = None
    reminder_at: str | int | float | None = None
    remind_minutes_before: int = 0
    timezone: str = DEFAULT_TZ


class VoiceTodoInput(BaseModel):
    session_id: str
    mark_ts: int
    text: str | None = None
    owner: str = "default"
    timezone: str = DEFAULT_TZ


class TodoUpdate(BaseModel):
    done: bool


class AgendaItemInput(BaseModel):
    title: str
    due_at: str | int | float | None = None
    assignee: str = "我"
    priority: str = "normal"
    remind_mode: str = "none"
    note: str = ""
    pinned: bool = False
    timezone: str = DEFAULT_TZ


class AgendaItemUpdate(BaseModel):
    title: str | None = None
    due_at: str | int | float | None = None
    clear_due: bool = False
    assignee: str | None = None
    priority: str | None = None
    remind_mode: str | None = None
    note: str | None = None
    pinned: bool | None = None
    done: bool | None = None
    timezone: str = DEFAULT_TZ


class AgendaBulkDelete(BaseModel):
    ids: list[str] = []
    completed: bool = False


class AgendaParseInput(BaseModel):
    text: str
    timezone: str = DEFAULT_TZ


class AgendaStore:
    def __init__(self, storage):
        self.storage, self.db = storage, storage.db

    def create_event(self, data: EventInput | dict) -> dict:
        d = data.model_dump() if isinstance(data, EventInput) else dict(data)
        if d.get("type", "todo") not in VALID_TYPES or d.get("source", "manual") not in VALID_SOURCES:
            raise ValueError("type 或 source 无效")
        if not str(d.get("title") or "").strip():
            raise ValueError("标题不能为空")
        start = parse_datetime(d["start"], d.get("timezone", DEFAULT_TZ))
        end = parse_datetime(d["end"], d.get("timezone", DEFAULT_TZ)) if d.get("end") is not None else None
        if end and end <= start:
            raise ValueError("end 必须晚于 start")
        owner = d.get("owner", "default")
        eid, created = _id(), _iso(datetime.now(timezone.utc))
        remind = parse_datetime(d["reminder_at"], d.get("timezone", DEFAULT_TZ)) if d.get("reminder_at") is not None else start - timedelta(minutes=max(0, int(d.get("remind_minutes_before", 0))))
        rid = _id()
        todo = None
        tid = None
        if d.get("type") == "todo" or d.get("source") == "voice":
            tid = _id()
            todo = {"id": tid, "text": d["title"].strip(), "due": _iso(start), "done": False}
        # event/reminder/todo/revision 是一个业务事实，必须同成同败。
        with self.db.transaction() as conn:
            conn.execute("INSERT INTO agenda_events VALUES(?,?,?,?,?,?,?,?,?,?)",
                         (eid, owner, d.get("type", "todo"), d["title"].strip(),
                          _iso(start), _iso(end) if end else None, d.get("recurrence_rule"),
                          d.get("source", "manual"), d.get("linked_meeting_id"), created))
            conn.execute("INSERT INTO agenda_reminders(id,event_id,remind_at,channel) VALUES(?,?,?,?)",
                         (rid, eid, _iso(remind), "screen"))
            if tid is not None:
                conn.execute("INSERT INTO agenda_todos(id,owner,text,due_at,done,source_event_id) VALUES(?,?,?,?,?,?)",
                             (tid, owner, d["title"].strip(), _iso(start), 0, eid))
            conn.execute(
                "INSERT INTO agenda_revisions(owner_user_id,revision,updated_at) VALUES(?,1,?)"
                " ON CONFLICT(owner_user_id) DO UPDATE SET revision=revision+1,"
                " updated_at=excluded.updated_at", (owner, created))
        return {"id": eid, "owner": owner, "type": d.get("type", "todo"),
                "title": d["title"].strip(), "start": _iso(start), "end": _iso(end) if end else None,
                "recurrence_rule": d.get("recurrence_rule"), "source": d.get("source", "manual"),
                "linked_meeting_id": d.get("linked_meeting_id"), "created_at": created,
                "reminder": {"id": rid, "event_id": eid, "remind_at": _iso(remind), "channel": "screen", "fired_at": None},
                "todo": todo}

    def bump_revision(self, owner: str) -> int:
        now = _iso(datetime.now(timezone.utc))
        self.db.execute(
            "INSERT INTO agenda_revisions(owner_user_id,revision,updated_at) VALUES(?,1,?)"
            " ON CONFLICT(owner_user_id) DO UPDATE SET revision=revision+1,updated_at=excluded.updated_at",
            (owner, now))
        row = self.db.query_one("SELECT revision FROM agenda_revisions WHERE owner_user_id=?", (owner,))
        return int(row["revision"])

    @staticmethod
    def _item(row) -> dict:
        item = dict(row)
        item["title"] = item.pop("text")
        item["done"] = bool(item["done"])
        item["pinned"] = bool(item.get("pinned"))
        return item

    @staticmethod
    def _validate_item(data: dict) -> None:
        if not str(data.get("title") or "").strip():
            raise ValueError("事项内容不能为空")
        if data.get("priority", "normal") not in {"normal", "important", "urgent"}:
            raise ValueError("优先级无效")
        if data.get("remind_mode", "none") not in {"none", "at_time", "10m", "1h", "1d"}:
            raise ValueError("提醒方式无效")

    def list_items(self, owner: str) -> dict:
        rows = self.db.query(
            "SELECT * FROM agenda_todos WHERE owner=?"
            " ORDER BY done ASC,pinned DESC,CASE WHEN due_at IS NULL THEN 1 ELSE 0 END,due_at,created_at",
            (owner,))
        items = [self._item(row) for row in rows]
        revision = self.db.query_one(
            "SELECT revision FROM agenda_revisions WHERE owner_user_id=?", (owner,))
        return {"items": items, "revision": int(revision["revision"]) if revision else 0}

    def create_item(self, owner: str, data: AgendaItemInput | dict) -> dict:
        d = data.model_dump() if isinstance(data, AgendaItemInput) else dict(data)
        self._validate_item(d)
        now = _iso(datetime.now(timezone.utc))
        due = parse_datetime(d["due_at"], d.get("timezone", DEFAULT_TZ)) if d.get("due_at") is not None else None
        due_iso = _iso(due) if due else None
        todo_id, event_id = _id(), None
        remind_at = self._remind_at(due, d.get("remind_mode", "none"))
        with self.db.transaction() as conn:
            if due:
                event_id = _id()
                conn.execute(
                    "INSERT INTO agenda_events(id,owner,type,title,start_at,end_at,recurrence_rule,source,linked_meeting_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (event_id, owner, "todo", d["title"].strip(), due_iso, None, None, "manual", None, now))
                if remind_at:
                    conn.execute(
                        "INSERT INTO agenda_reminders(id,event_id,remind_at,channel) VALUES(?,?,?,?)",
                        (_id(), event_id, remind_at, "screen"))
            conn.execute(
                "INSERT INTO agenda_todos(id,owner,text,due_at,done,source_event_id,assignee,priority,remind_mode,remind_at,note,pinned,completed_at,created_at,updated_at)"
                " VALUES(?,?,?,?,0,?,?,?,?,?,?,?,NULL,?,?)",
                (todo_id, owner, d["title"].strip(), due_iso, event_id,
                 str(d.get("assignee") or "我").strip(), d.get("priority", "normal"),
                 d.get("remind_mode", "none"), remind_at, str(d.get("note") or "").strip(),
                 int(bool(d.get("pinned"))), now, now))
            self._bump_in(conn, owner, now)
        return self._item(self.db.query_one("SELECT * FROM agenda_todos WHERE id=?", (todo_id,)))

    @staticmethod
    def _remind_at(due: datetime | None, mode: str) -> str | None:
        if due is None or mode == "none":
            return None
        delta = {"at_time": 0, "10m": 10, "1h": 60, "1d": 1440}.get(mode)
        return _iso(due - timedelta(minutes=delta)) if delta is not None else None

    @staticmethod
    def _bump_in(conn, owner: str, now: str) -> None:
        conn.execute(
            "INSERT INTO agenda_revisions(owner_user_id,revision,updated_at) VALUES(?,1,?)"
            " ON CONFLICT(owner_user_id) DO UPDATE SET revision=revision+1,updated_at=excluded.updated_at",
            (owner, now))

    def update_item(self, owner: str, item_id: str, patch: AgendaItemUpdate) -> dict | None:
        row = self.db.query_one("SELECT * FROM agenda_todos WHERE id=? AND owner=?", (item_id, owner))
        if not row:
            return None
        current = self._item(row)
        values = patch.model_dump(exclude_unset=True)
        title = values.get("title", current["title"])
        priority = values.get("priority", current.get("priority") or "normal")
        remind_mode = values.get("remind_mode", current.get("remind_mode") or "none")
        self._validate_item({"title": title, "priority": priority, "remind_mode": remind_mode})
        if values.get("clear_due"):
            due = None
        elif "due_at" in values:
            due = parse_datetime(values["due_at"], values.get("timezone", DEFAULT_TZ)) if values["due_at"] is not None else None
        else:
            due = datetime.fromisoformat(current["due_at"]) if current.get("due_at") else None
        due_iso = _iso(due) if due else None
        remind_at = self._remind_at(due, remind_mode)
        done = bool(values.get("done", current["done"]))
        completed_at = _iso(datetime.now(timezone.utc)) if done and not current["done"] else (None if not done else current.get("completed_at"))
        now = _iso(datetime.now(timezone.utc))
        event_id = current.get("source_event_id")
        with self.db.transaction() as conn:
            if due and event_id:
                conn.execute("UPDATE agenda_events SET title=?,start_at=? WHERE id=? AND owner=?", (title.strip(), due_iso, event_id, owner))
            elif due and not event_id:
                event_id = _id()
                conn.execute(
                    "INSERT INTO agenda_events(id,owner,type,title,start_at,end_at,recurrence_rule,source,linked_meeting_id,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (event_id, owner, "todo", title.strip(), due_iso, None, None, "manual", None, now))
            elif not due and event_id:
                conn.execute("DELETE FROM agenda_events WHERE id=? AND owner=?", (event_id, owner))
                event_id = None
            if event_id:
                conn.execute("DELETE FROM agenda_reminders WHERE event_id=?", (event_id,))
                if remind_at:
                    conn.execute("INSERT INTO agenda_reminders(id,event_id,remind_at,channel) VALUES(?,?,?,?)", (_id(), event_id, remind_at, "screen"))
            conn.execute(
                "UPDATE agenda_todos SET text=?,due_at=?,source_event_id=?,assignee=?,priority=?,remind_mode=?,remind_at=?,note=?,pinned=?,done=?,completed_at=?,updated_at=?"
                " WHERE id=? AND owner=?",
                (title.strip(), due_iso, event_id, str(values.get("assignee", current.get("assignee") or "我")).strip(),
                 priority, remind_mode, remind_at, str(values.get("note", current.get("note") or "")).strip(),
                 int(bool(values.get("pinned", current["pinned"]))), int(done), completed_at, now, item_id, owner))
            self._bump_in(conn, owner, now)
        return self._item(self.db.query_one("SELECT * FROM agenda_todos WHERE id=?", (item_id,)))

    def delete_items(self, owner: str, ids: list[str] | None = None, completed: bool = False) -> int:
        rows = self.db.query("SELECT id,source_event_id FROM agenda_todos WHERE owner=?", (owner,))
        wanted = set(ids or [])
        if completed:
            done_ids = {r["id"] for r in self.db.query("SELECT id FROM agenda_todos WHERE owner=? AND done=1", (owner,))}
            wanted |= done_ids
        selected = [r for r in rows if r["id"] in wanted]
        if not selected:
            return 0
        now = _iso(datetime.now(timezone.utc))
        with self.db.transaction() as conn:
            conn.executemany("DELETE FROM agenda_todos WHERE id=? AND owner=?", [(r["id"], owner) for r in selected])
            event_ids = [(r["source_event_id"], owner) for r in selected if r["source_event_id"]]
            if event_ids:
                conn.executemany("DELETE FROM agenda_events WHERE id=? AND owner=?", event_ids)
            self._bump_in(conn, owner, now)
        return len(selected)

    def _occurs(self, row, day, tz) -> bool:
        start = datetime.fromisoformat(row["start_at"]).astimezone(tz)
        rule = (row["recurrence_rule"] or "").upper()
        if not rule:
            return start.date() == day
        if rule in {"DAILY", "FREQ=DAILY"}:
            return day >= start.date()
        if rule in {"WEEKLY", "FREQ=WEEKLY"}:
            return day >= start.date() and day.weekday() == start.weekday()
        byday = re.search(r"BYDAY=([A-Z,]+)", rule)
        names = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
        return day >= start.date() and bool(byday and names[day.weekday()] in byday.group(1).split(","))

    def today(self, owner="default", date=None, tz_name=DEFAULT_TZ) -> dict:
        tz = get_timezone(tz_name)
        day = datetime.fromisoformat(date).date() if date else datetime.now(tz).date()
        rows = self.db.query("SELECT * FROM agenda_events WHERE owner=? ORDER BY start_at", (owner,))
        events = []
        for r in rows:
            if not self._occurs(r, day, tz):
                continue
            item = dict(r)
            base = datetime.fromisoformat(item.pop("start_at")).astimezone(tz)
            occurrence = base.replace(year=day.year, month=day.month, day=day.day) if r["recurrence_rule"] else base
            item["start"] = occurrence.isoformat()
            item["end"] = item.pop("end_at")
            events.append(item)
        ids = [e["id"] for e in events]
        reminders = [] if not ids else [dict(r) for r in self.db.query(
            f"SELECT * FROM agenda_reminders WHERE event_id IN ({','.join('?' for _ in ids)}) ORDER BY remind_at", tuple(ids))]
        event_by_id = {e["id"]: e for e in events}
        row_by_id = {r["id"]: r for r in rows}
        for reminder in reminders:
            original = row_by_id[reminder["event_id"]]
            if original["recurrence_rule"]:
                delta = (datetime.fromisoformat(reminder["remind_at"])
                         - datetime.fromisoformat(original["start_at"]))
                occurrence = datetime.fromisoformat(event_by_id[reminder["event_id"]]["start"])
                reminder["remind_at"] = _iso(occurrence + delta)
        todos = [dict(r) | {"done": bool(r["done"])} for r in self.db.query(
            "SELECT * FROM agenda_todos WHERE owner=? ORDER BY due_at", (owner,))]
        return {"date": day.isoformat(), "timezone": tz_name, "events": events,
                "reminders": reminders, "todos": todos}

    def source_text(self, session_id: str, mark_ts: int, owner_user_id: str | None = None) -> str:
        if owner_user_id is not None and not self.storage.user_owns_meeting(session_id, owner_user_id):
            return ""
        segs = self.storage.load_segments(session_id)
        if not segs:
            return ""
        nearest = min(segs, key=lambda s: abs(int(s.get("start_ms", 0)) - mark_ts))
        return str(nearest.get("text") or "")


def create_agenda_router(storage, *, prefix: str = "/api/v1/agenda") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["agenda"])
    store = AgendaStore(storage)

    @router.post("/events")
    async def create_event(body: EventInput, user: CurrentUser = Depends(require_auth)):
        try:
            return store.create_event(body.model_dump() | {"owner": user.id})
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/today")
    async def today(date: str | None = Query(None), timezone: str = DEFAULT_TZ,
                    user: CurrentUser = Depends(require_auth)):
        try:
            return store.today(user.id, date, timezone)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.get("/items")
    async def list_items(user: CurrentUser = Depends(require_auth)):
        return store.list_items(user.id)

    @router.post("/items")
    async def create_item(body: AgendaItemInput, user: CurrentUser = Depends(require_auth)):
        try:
            return store.create_item(user.id, body)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @router.patch("/items/{item_id}")
    async def update_item(item_id: str, body: AgendaItemUpdate,
                          user: CurrentUser = Depends(require_auth)):
        try:
            item = store.update_item(user.id, item_id, body)
        except (ValueError, KeyError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if item is None:
            raise HTTPException(status_code=404, detail="事项不存在")
        return item

    @router.delete("/items/{item_id}")
    async def delete_item(item_id: str, user: CurrentUser = Depends(require_auth)):
        if not store.db.query_one("SELECT id FROM agenda_todos WHERE id=? AND owner=?", (item_id, user.id)):
            raise HTTPException(status_code=404, detail="事项不存在")
        return {"deleted": store.delete_items(user.id, [item_id])}

    @router.post("/items/bulk-delete")
    async def bulk_delete_items(body: AgendaBulkDelete,
                                user: CurrentUser = Depends(require_auth)):
        return {"deleted": store.delete_items(user.id, body.ids, body.completed)}

    @router.post("/items/parse")
    async def parse_item(body: AgendaParseInput, user: CurrentUser = Depends(require_auth)):
        del user
        text = body.text.strip()
        if not text:
            raise HTTPException(status_code=400, detail="请输入事项")
        now = datetime.now(get_timezone(body.timezone))
        extracted = None
        try:
            extracted = await _llm.extract_agenda(text, now.isoformat(), body.timezone)
        except Exception:
            extracted = None
        if extracted:
            raw_due = extracted.get("start") or extracted.get("due_at")
            try:
                due_at = _iso(parse_datetime(raw_due, body.timezone)) if raw_due else None
            except (ValueError, TypeError):
                due_at = None
            return {"title": clean_voice_todo_content(str(extracted.get("title") or text)),
                    "due_at": due_at, "assignee": "我", "priority": "normal",
                    "remind_mode": "at_time" if due_at else "none", "note": ""}
        fallback = extract_optional_voice_todo(text, now, body.timezone)
        return {"title": fallback["title"],
                "due_at": _iso(fallback["start"]) if fallback.get("start") else None,
                "assignee": "我", "priority": "normal",
                "remind_mode": "at_time" if fallback.get("start") else "none", "note": ""}

    @router.post("/voice-todo")
    async def voice_todo(body: VoiceTodoInput, user: CurrentUser = Depends(require_auth)):
        meeting_owner = storage.meeting_owner(body.session_id)
        if meeting_owner is not None and meeting_owner != user.id:
            raise HTTPException(status_code=404, detail="会议不存在")
        existing = store.db.query_one("SELECT event_id FROM agenda_voice_captures WHERE session_id=? AND mark_ts=?",
                                      (body.session_id, body.mark_ts))
        if existing:
            return {"event_id": existing["event_id"], "duplicate": True}
        text = (body.text or store.source_text(body.session_id, body.mark_ts, user.id)).strip()
        try:
            now = datetime.now(get_timezone(body.timezone))
            extracted = await _llm.extract_agenda(text, now.isoformat(), body.timezone)
            if extracted:
                extracted = {"title": str(extracted.get("title") or "语音待办"),
                             "type": extracted.get("type") if extracted.get("type") in VALID_TYPES else "todo",
                             "start": parse_datetime(extracted.get("start"), body.timezone)}
            else:
                extracted = extract_voice_todo(text, now=now, tz_name=body.timezone)
            event = store.create_event({"owner": user.id, "type": extracted["type"], "title": extracted["title"],
                                        "start": extracted["start"], "source": "voice",
                                        "linked_meeting_id": body.session_id, "timezone": body.timezone})
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        store.db.execute("INSERT INTO agenda_voice_captures VALUES(?,?,?,?,?,?)",
                         (_id(), body.session_id, body.mark_ts, text, event["id"], _iso(datetime.now(timezone.utc))))
        return {"event": event, "text": text, "duplicate": False}

    @router.patch("/todos/{todo_id}")
    async def update_todo(todo_id: str, body: TodoUpdate,
                          user: CurrentUser = Depends(require_auth)):
        now = _iso(datetime.now(timezone.utc))
        # The todo flag and device-visible agenda revision are one commit.  A
        # crash can therefore never leave the web state newer than the device
        # snapshot (or vice versa).
        with store.db.transaction() as conn:
            cur = conn.execute("UPDATE agenda_todos SET done=?,completed_at=?,updated_at=? WHERE id=? AND owner=?",
                               (int(body.done), now if body.done else None, now, todo_id, user.id))
            if cur.rowcount == 0:
                raise HTTPException(status_code=404, detail="待办不存在")
            conn.execute(
                "INSERT INTO agenda_revisions(owner_user_id,revision,updated_at) VALUES(?,1,?)"
                " ON CONFLICT(owner_user_id) DO UPDATE SET revision=revision+1,"
                " updated_at=excluded.updated_at", (user.id, now))
        return {"id": todo_id, "done": body.done}

    router.agenda_store = store
    return router
