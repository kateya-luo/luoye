import asyncio
import tempfile
import unittest
from pathlib import Path

from app.meeting_memory import MeetingMemory
from app.minutes_service import MinutesCreate, MinutesService
from app.storage import Storage


class FakeMinutesLLM:
    def __init__(self):
        self.calls = 0

    async def generate_template_minutes(self, transcript, template, **kwargs):
        self.calls += 1
        return ({
            "schema_version": 1,
            "template": {"id": template["id"], "version": 1, "name": template["name"]},
            "title": "测试会议", "summary": "这是一次完整会后纪要。",
            "sections": [{"heading": "会议摘要", "items": ["事实一"]}],
            "conclusions": {"decisions": [], "consensus": [], "tendencies": [],
                            "suggestions": [], "disagreements": [], "unresolved": []},
            "decisions": [], "action_items": [], "participants": [],
            "memory_candidates": [], "mindmap": {"title": "测试会议", "branches": []},
        }, {"model": "fake", "prompt_tokens": 10, "completion_tokens": 20,
            "cache_hit_tokens": 0, "elapsed_ms": 1, "finish_reason": "stop"})


class MinutesServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.storage = Storage(Path(self.temp.name))
        now = "2026-08-27T00:00:00+00:00"
        self.storage.db.execute(
            "INSERT INTO users(id,username,password_hash,created_at,updated_at) VALUES('TEST1','test1','x',?,?)",
            (now, now))
        self.storage.create_meeting("m1", owner_user_id="TEST1")
        self.storage.upsert_segment("m1", {"seg_id": "s1", "start_ms": 0, "end_ms": 2000,
                                                   "text": "完整转写", "speaker_id": "spk_01",
                                                   "speaker_label": "说话人 1", "state": "final"})
        self.storage.set_state("m1", "transcript_ready")
        self.memory = MeetingMemory(self.storage)
        self.service = MinutesService(self.storage, self.memory)
        self.fake = FakeMinutesLLM()
        self.service.llm = self.fake

    async def asyncTearDown(self):
        await self.service.stop()
        self.storage.db.close()
        self.temp.cleanup()

    async def _ready(self, job_id):
        for _ in range(100):
            job = self.service._job(job_id)
            if job["state"] in {"ready", "failed"}:
                return job
            await asyncio.sleep(0.01)
        self.fail("minutes job timed out")

    async def test_thirty_templates_and_double_click_is_one_model_call(self):
        self.assertEqual(len(self.service.list_templates()), 30)
        first = self.service.create_job("m1", "TEST1", MinutesCreate(template_id="16"))
        second = self.service.create_job("m1", "TEST1", MinutesCreate(template_id="16"))
        self.assertEqual(first["id"], second["id"])
        job = await self._ready(first["id"])
        self.assertEqual(job["state"], "ready")
        self.assertEqual(self.fake.calls, 1)
        meeting = self.storage.get_meeting("m1", "TEST1")
        self.assertEqual(meeting["minutes_status"], "ready")
        self.assertEqual(meeting["summary"]["template"]["id"], "16")

    async def test_no_job_means_no_model_call(self):
        await asyncio.sleep(0.02)
        self.assertEqual(self.fake.calls, 0)
        self.assertEqual(self.storage.get_meeting("m1")["minutes_status"], "not_created")

    async def test_manual_person_memory_is_owner_scoped(self):
        async def no_voice(*args, **kwargs):
            return None
        self.memory.enroll_voiceprint = no_voice
        assignment = await self.memory.assign_speaker(
            "m1", "TEST1", "spk_01", display_name="骆洲", role="负责人", remember=True)
        self.assertEqual(assignment["display_name"], "骆洲")
        self.assertTrue(assignment["remembered"])
        people = self.memory.list_people("TEST1")
        self.assertEqual([p["display_name"] for p in people], ["骆洲"])
        self.assertEqual(self.memory.list_people("TEST2"), [])


if __name__ == "__main__":
    unittest.main()
