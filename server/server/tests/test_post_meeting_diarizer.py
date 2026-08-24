import os
import struct
import tempfile
import unittest
from pathlib import Path

from app.post_meeting_diarizer import PostMeetingDiarizer
from app.speaker_diarizer import SpeakerDiarizer


class PostMeetingDiarizerTest(unittest.IsolatedAsyncioTestCase):
    async def test_live_diarizer_reports_backend_failure(self):
        old_environment = os.environ.copy()
        self.addCleanup(self._restore_environment, old_environment)
        os.environ["SPEAKER_MODE"] = "remote"
        diarizer = SpeakerDiarizer()

        async def fail(_pcm):
            raise RuntimeError("speaker unavailable")

        diarizer._fetch_embedding = fail
        self.assertIsNone(await diarizer.assign(struct.pack("<h", 1000) * (diarizer.min_bytes // 2)))
        self.assertEqual(1, diarizer.diagnostics()["failed_segments"])
        self.assertEqual("speaker unavailable", diarizer.diagnostics()["last_error"])

    async def test_live_new_speaker_requires_confirmation_without_a_people_cap(self):
        old_environment = os.environ.copy()
        self.addCleanup(self._restore_environment, old_environment)
        os.environ.update({
            "SPEAKER_MODE": "remote",
            "SPEAKER_MIN_SEGMENT_SECONDS": "0.5",
            "SPEAKER_MIN_RMS": "0",
            "SPEAKER_SIMILARITY_THRESHOLD": "0.68",
            "SPEAKER_CANDIDATE_CONFIRMATIONS": "2",
        })
        diarizer = SpeakerDiarizer()
        vectors = [[1.0, 0.0]]
        for speaker in range(1, 10):
            vector = [0.0] * 10
            vector[speaker] = 1.0
            vectors.extend([vector, vector])

        async def next_embedding(_pcm):
            return vectors.pop(0)

        diarizer._fetch_embedding = next_embedding
        pcm = struct.pack("<h", 1000) * (diarizer.min_bytes // 2)
        self.assertEqual("spk_01", await diarizer.assign(pcm))
        for expected in range(2, 11):
            self.assertIsNone(await diarizer.assign(pcm))
            self.assertEqual(f"spk_{expected:02d}", await diarizer.assign(pcm))
        self.assertEqual(10, diarizer.diagnostics()["confirmed_speakers"])

    async def test_live_diarizer_bounds_long_input_to_loudest_window(self):
        old_environment = os.environ.copy()
        self.addCleanup(self._restore_environment, old_environment)
        os.environ.update({
            "SPEAKER_MODE": "remote",
            "SPEAKER_MIN_SEGMENT_SECONDS": "1",
            "SPEAKER_MAX_SEGMENT_SECONDS": "2",
            "SPEAKER_MIN_RMS": "80",
        })
        diarizer = SpeakerDiarizer()
        captured = []

        async def capture(chunk):
            captured.append(chunk)
            return [1.0, 0.0]

        diarizer._fetch_embedding = capture
        one_second = diarizer.sample_rate
        pcm = (struct.pack("<h", 100) * (2 * one_second)
               + struct.pack("<h", 1000) * (2 * one_second)
               + struct.pack("<h", 200) * (2 * one_second))
        self.assertEqual("spk_01", await diarizer.assign(pcm))
        self.assertEqual(diarizer.max_bytes, len(captured[0]))
        self.assertEqual(1000, struct.unpack_from("<h", captured[0])[0])

    async def test_globally_corrects_seven_speakers(self):
        old_environment = os.environ.copy()
        self.addCleanup(self._restore_environment, old_environment)
        os.environ.update({
            "SPEAKER_MODE": "remote",
            "SPEAKER_MIN_SEGMENT_SECONDS": "0.8",
            "POST_MEETING_DIARIZATION_ENABLED": "true",
        })
        diarizer = PostMeetingDiarizer()
        segments = []
        pcm = bytearray()
        for turn in range(3):
            for speaker in range(7):
                start_ms = len(pcm) // 32
                pcm.extend(struct.pack("<h", 1000 + speaker * 1000) * 16000)
                end_ms = len(pcm) // 32
                segments.append({
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "text": f"speaker {speaker + 1} turn {turn + 1}",
                    "speaker_id": f"spk_{turn * 7 + speaker + 1:02d}",
                })

        async def fake_embedding(_client, chunk):
            sample = struct.unpack_from("<h", chunk)[0]
            speaker = (sample - 1000) // 1000
            vector = [0.0] * 7
            vector[speaker] = 1.0
            return vector

        diarizer._fetch_embedding = fake_embedding
        with tempfile.TemporaryDirectory() as temporary_directory:
            audio_path = Path(temporary_directory) / "meeting.pcm"
            audio_path.write_bytes(pcm)
            result = await diarizer.correct(audio_path, segments)

        self.assertEqual("corrected", result["status"])
        self.assertEqual(7, result["speaker_count"])
        self.assertEqual([3] * 7, [item["segment_count"] for item in result["speakers"]])
        self.assertEqual("\u8bf4\u8bdd\u4eba 1", result["segments"][0]["speaker_label"])
        self.assertEqual("\u8bf4\u8bdd\u4eba 7", result["segments"][6]["speaker_label"])
        self.assertEqual("spk_01", result["segments"][7]["speaker_id"])
        self.assertTrue(all("realtime_speaker_id" in item for item in result["segments"]))

    @staticmethod
    def _restore_environment(environment):
        os.environ.clear()
        os.environ.update(environment)


if __name__ == "__main__":
    unittest.main()
