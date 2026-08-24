import asyncio
import os
import unittest

from app.funasr_offline_client import FunASROfflineClient, parse_timestamp


class ParseTimestampTest(unittest.TestCase):
    def test_list_form(self):
        self.assertEqual(parse_timestamp([[480, 600], [600, 840]]), (480, 840))

    def test_json_string_form(self):
        self.assertEqual(parse_timestamp("[[480,600],[600,840]]"), (480, 840))

    def test_garbage_returns_none(self):
        self.assertIsNone(parse_timestamp(None))
        self.assertIsNone(parse_timestamp(""))
        self.assertIsNone(parse_timestamp("not-json"))
        self.assertIsNone(parse_timestamp([]))

    def test_mock_transcribe_offsets(self):
        os.environ["ASR_MODE"] = "mock"
        pcm = b"\x00\x00" * 16000  # 1s @16k/16bit
        segs = asyncio.run(FunASROfflineClient().transcribe(pcm, base_offset_ms=900000))
        self.assertEqual(len(segs), 1)
        self.assertEqual(segs[0].start_ms, 900000)
        self.assertEqual(segs[0].end_ms, 901000)
        self.assertEqual(segs[0].source, "offline")
        self.assertEqual(segs[0].state, "final")


if __name__ == "__main__":
    unittest.main()
