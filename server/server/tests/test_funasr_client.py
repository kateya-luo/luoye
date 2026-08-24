import json
import unittest

from app.funasr_client import FunASRClient


class _Messages:
    def __init__(self, messages):
        self._messages = list(messages)

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._messages:
            raise StopAsyncIteration
        return json.dumps(self._messages.pop(0), ensure_ascii=False)


class FunASRFragmentAggregationTest(unittest.IsolatedAsyncioTestCase):
    async def test_online_fragments_expose_cumulative_partial_and_reset_on_final(self):
        client = FunASRClient()
        client.mock = False
        client.websocket = _Messages([
            {"text": "现在我们", "mode": "2pass-online"},
            {"text": "讨论一下", "mode": "2pass-online"},
            {"text": "上传速度", "mode": "2pass-online"},
            {"text": "现在我们讨论一下上传速度。", "mode": "2pass-offline"},
            {"text": "下一句", "mode": "2pass-online"},
        ])

        await client._receive_loop()

        self.assertEqual(client.online_parts, ["下一句"])
        results = client._drain_results()
        self.assertEqual([item["text"] for item in results], [
            "现在我们", "讨论一下", "上传速度", "现在我们讨论一下上传速度。", "下一句"])
        self.assertEqual([item["partial_text"] for item in results], [
            "现在我们", "现在我们讨论一下", "现在我们讨论一下上传速度", "", "下一句"])


if __name__ == "__main__":
    unittest.main()
