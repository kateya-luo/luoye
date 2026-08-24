import json
import unittest
from unittest.mock import patch

from app.deepseek_client import DeepSeekClient


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        payload = {
            "summary": "摘要", "decisions": [], "action_items": [],
            "mindmap": {"title": "主题", "branches": []},
            "timeline_chapters": [{
                "anchor": "S0002",
                "start_ms": 12_000, "end_ms": 20_000,
                "title": "交付决策", "items": ["周五交付"],
                "boundary": {"kind": "decision_phase", "confidence": 1.7,
                             "reason": "从方案讨论转入交付决策"},
            }],
        }
        return {"choices": [{"message": {"content": json.dumps(payload,
                                                                  ensure_ascii=False)}}]}


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


class DeepSeekTimelineProtocolTest(unittest.IsolatedAsyncioTestCase):
    async def test_prompt_and_parser_preserve_semantic_boundary_evidence(self):
        fake = _AsyncClient()
        client = DeepSeekClient()
        client.key = "test-only"
        with patch("app.deepseek_client.httpx.AsyncClient", return_value=fake):
            result = await client.summarize(
                "[t=12000ms] 转入交付决策", rolling=True,
                source_language="zh", output_language="zh")
        system = fake.request[1]["json"]["messages"][0]["content"]
        self.assertIn("Do not target a fixed interval", system)
        self.assertIn("incremental detail within the same topic is NEVER", system)
        self.assertIn('boundary.kind="mark"', system)
        self.assertIn("anchor as authoritative", system)
        self.assertEqual(result["timeline_chapters"][0]["anchor"], "S0002")
        boundary = result["timeline_chapters"][0]["boundary"]
        self.assertEqual(boundary["kind"], "decision_phase")
        self.assertEqual(boundary["confidence"], 1.0)
        self.assertIn("交付决策", boundary["reason"])


if __name__ == "__main__":
    unittest.main()
