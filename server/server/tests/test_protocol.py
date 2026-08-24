import unittest

from app.protocol import MessageType, event


class ProtocolEventTest(unittest.TestCase):
    def test_enum_event(self):
        self.assertEqual(
            event(MessageType.ASR_RESULT, text="hello"),
            {"type": "asr_result", "text": "hello"},
        )

    def test_extension_string_event(self):
        self.assertEqual(
            event("observer_catchup", segments=[]),
            {"type": "observer_catchup", "segments": []},
        )


if __name__ == "__main__":
    unittest.main()
