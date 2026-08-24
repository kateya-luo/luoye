import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from datetime import timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.agenda import (AgendaStore, EventInput, create_agenda_router,
                        extract_optional_voice_todo, extract_voice_todo)
from app.storage import Storage


class VoiceExtractionTest(unittest.TestCase):
    def setUp(self):
        self.now = datetime(2026, 7, 12, 10, 0, tzinfo=timezone(timedelta(hours=8)))

    def test_chinese_evening_time(self):
        item = extract_voice_todo("晚上七点学生会开会", self.now)
        self.assertEqual(item["start"].isoformat(), "2026-07-12T19:00:00+08:00")
        self.assertEqual(item["title"], "学生会开会")
        self.assertEqual(item["type"], "meeting")

    def test_english_tomorrow_pm(self):
        item = extract_voice_todo("tomorrow 7 pm team meeting", self.now)
        self.assertEqual(item["start"].isoformat(), "2026-07-13T19:00:00+08:00")
        self.assertEqual(item["type"], "meeting")

    def test_past_time_rolls_to_next_day(self):
        item = extract_voice_todo("上午九点交作业", self.now)
        self.assertEqual(item["start"].date().isoformat(), "2026-07-13")

    def test_period_without_clock_uses_documented_default(self):
        item = extract_optional_voice_todo("明天上午提醒我要做交付包装材料", self.now)
        self.assertEqual(item["start"].isoformat(), "2026-07-13T09:00:00+08:00")
        self.assertEqual(item["title"], "交付包装材料")

    def test_action_without_time_is_preserved(self):
        item = extract_optional_voice_todo(
            "有个事情你帮我记一下，之后要记得交付包装材料", self.now)
        self.assertIsNone(item["start"])
        self.assertEqual(item["title"], "交付包装材料")


class AgendaStoreTest(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.store = AgendaStore(Storage(Path(self.temp)))

    def tearDown(self):
        self.store.db.close()

    def test_create_event_todo_and_reminder(self):
        result = self.store.create_event(EventInput(
            type="todo", title="交报告", start="2026-07-12T19:00:00+08:00", source="manual"))
        self.assertEqual(result["title"], "交报告")
        self.assertIsNotNone(result["todo"])
        today = self.store.today(date="2026-07-12")
        self.assertEqual(len(today["events"]), 1)
        self.assertEqual(len(today["reminders"]), 1)
        self.assertEqual(len(today["todos"]), 1)

    def test_weekly_recurrence_expands_only_matching_day(self):
        self.store.create_event(EventInput(
            type="class", title="英语课", start="2026-07-13T09:00:00+08:00",
            recurrence_rule="FREQ=WEEKLY;BYDAY=MO,WE"))
        self.assertEqual(len(self.store.today(date="2026-07-15")["events"]), 1)
        self.assertEqual(len(self.store.today(date="2026-07-16")["events"]), 0)

    def test_timezone_boundary(self):
        self.store.create_event(EventInput(
            type="meeting", title="跨日会议", start="2026-07-12T16:30:00Z"))
        self.assertEqual(len(self.store.today(date="2026-07-13", tz_name="Asia/Shanghai")["events"]), 1)


class AgendaApiTest(unittest.TestCase):
    def setUp(self):
        self.temp = self.enterContext(tempfile.TemporaryDirectory())
        self.storage = Storage(Path(self.temp))
        app = FastAPI()
        app.include_router(create_agenda_router(self.storage))
        self.client = TestClient(app)

    def tearDown(self):
        self.storage.db.close()

    def test_three_contract_endpoints(self):
        created = self.client.post("/api/v1/agenda/events", json={
            "type": "class", "title": "英语课", "start": "2026-07-13T09:00:00+08:00",
            "recurrence_rule": "FREQ=WEEKLY;BYDAY=MO",
        })
        self.assertEqual(created.status_code, 200)
        today = self.client.get("/api/v1/agenda/today", params={"date": "2026-07-13"})
        self.assertEqual(today.status_code, 200)
        self.assertEqual(today.json()["events"][0]["title"], "英语课")

        voice = self.client.post("/api/v1/agenda/voice-todo", json={
            "session_id": "meeting-1", "mark_ts": 12000, "text": "晚上七点学生会开会",
        })
        self.assertEqual(voice.status_code, 200)
        self.assertEqual(voice.json()["event"]["title"], "学生会开会")

    def test_voice_mark_is_idempotent(self):
        body = {"session_id": "meeting-2", "mark_ts": 5000, "text": "晚上八点交报告"}
        first = self.client.post("/api/v1/agenda/voice-todo", json=body).json()
        second = self.client.post("/api/v1/agenda/voice-todo", json=body).json()
        self.assertFalse(first["duplicate"])
        self.assertTrue(second["duplicate"])
        self.assertEqual(second["event_id"], first["event"]["id"])

    def test_v015_unified_item_lifecycle_and_revision(self):
        created = self.client.post("/api/v1/agenda/items", json={
            "title": "联系包装供应商", "assignee": "罗工",
            "priority": "important", "remind_mode": "none",
        })
        self.assertEqual(created.status_code, 200, created.text)
        item = created.json()
        self.assertIsNone(item["due_at"])
        self.assertEqual(item["assignee"], "罗工")
        first_revision = self.client.get("/api/v1/agenda/items").json()["revision"]

        updated = self.client.patch(f"/api/v1/agenda/items/{item['id']}", json={
            "due_at": "2026-08-12T15:00:00+08:00", "remind_mode": "10m",
            "pinned": True,
        })
        self.assertEqual(updated.status_code, 200, updated.text)
        self.assertTrue(updated.json()["pinned"])
        self.assertIsNotNone(updated.json()["source_event_id"])

        completed = self.client.patch(
            f"/api/v1/agenda/items/{item['id']}", json={"done": True})
        self.assertTrue(completed.json()["done"])
        self.assertIsNotNone(completed.json()["completed_at"])
        restored = self.client.patch(
            f"/api/v1/agenda/items/{item['id']}", json={"done": False})
        self.assertFalse(restored.json()["done"])
        self.assertGreater(self.client.get("/api/v1/agenda/items").json()["revision"], first_revision)

        deleted = self.client.delete(f"/api/v1/agenda/items/{item['id']}")
        self.assertEqual(deleted.json()["deleted"], 1)
        self.assertEqual(self.client.get("/api/v1/agenda/items").json()["items"], [])

    def test_v015_bulk_clear_completed_only(self):
        active = self.client.post("/api/v1/agenda/items", json={"title": "保留"}).json()
        finished = self.client.post("/api/v1/agenda/items", json={"title": "清理"}).json()
        self.client.patch(f"/api/v1/agenda/items/{finished['id']}", json={"done": True})
        cleared = self.client.post("/api/v1/agenda/items/bulk-delete", json={"completed": True})
        self.assertEqual(cleared.json()["deleted"], 1)
        remaining = self.client.get("/api/v1/agenda/items").json()["items"]
        self.assertEqual([item["id"] for item in remaining], [active["id"]])


if __name__ == "__main__":
    unittest.main()
