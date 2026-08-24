import json
import unittest
from unittest.mock import patch

from app.deepseek_client import DeepSeekClient


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"choices": [{"message": {"content": json.dumps({
            "content": "交付包装材料",
            "has_time": True,
            "due_at": "2026-08-11T09:00:00+08:00",
            "time_text": "明天上午九点",
            "type": "reminder",
            "confidence": 0.98,
        }, ensure_ascii=False)}}]}


class _AsyncClient:
    def __init__(self):
        self.request = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.request = (url, kwargs)
        return _Response()


class DeepSeekTodoProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_time_and_content_are_independent_fields(self):
        fake = _AsyncClient()
        client = DeepSeekClient()
        client.key = "test-only"
        with patch("app.deepseek_client.httpx.AsyncClient", return_value=fake):
            result = await client.extract_todo(
                "明天上午提醒我要交付包装材料",
                "2026-08-10T13:30:00+08:00", "Asia/Shanghai")
        self.assertEqual(result["content"], "交付包装材料")
        self.assertEqual(result["due_at"], "2026-08-11T09:00:00+08:00")
        system = fake.request[1]["json"]["messages"][0]["content"]
        self.assertIn("optional time and required action content", system)
        self.assertIn("上午=09:00", system)
        self.assertIn("has_time=false", system)

    async def test_no_key_uses_deterministic_fallback(self):
        client = DeepSeekClient()
        client.key = ""
        self.assertIsNone(await client.extract_todo(
            "帮我记一下交付材料", "2026-08-10T13:30:00+08:00", "Asia/Shanghai"))


if __name__ == "__main__":
    unittest.main()
