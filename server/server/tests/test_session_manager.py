import time
import tempfile
import unittest
from pathlib import Path

from app.session_manager import SessionInUseError, SessionManager, SessionOwnershipError


class SessionManagerTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = self.enterContext(tempfile.TemporaryDirectory())
        self.audio_root = Path(self.temp_dir)

    def test_suspended_session_resumes_without_truncating_audio(self):
        manager = SessionManager(self.audio_root, resume_window_seconds=7200)
        session, resumed = manager.create("meeting-1")
        self.assertFalse(resumed)
        session.audio.append(b"first")
        session.audio.close()
        manager.suspend(session.id, session)

        resumed_session, resumed = manager.create(session.id)
        self.assertTrue(resumed)
        self.assertIs(resumed_session, session)
        resumed_session.audio.append(b"-second")
        resumed_session.audio.close()

        self.assertEqual(resumed_session.audio.path.read_bytes(), b"first-second")

    def test_active_session_cannot_be_taken_over(self):
        manager = SessionManager(self.audio_root, resume_window_seconds=7200)
        manager.create("meeting-1")

        with self.assertRaises(SessionInUseError):
            manager.create("meeting-1")

    def test_expired_session_starts_fresh_and_truncates_stale_audio(self):
        manager = SessionManager(self.audio_root, resume_window_seconds=1)
        session, _ = manager.create("meeting-1")
        session.audio.append(b"stale")
        session.audio.close()
        manager.suspend(session.id, session)
        session.disconnected_at = time.time() - 2

        fresh, resumed = manager.create(session.id)
        self.assertFalse(resumed)
        fresh.audio.append(b"fresh")
        fresh.audio.close()

        self.assertEqual(fresh.audio.path.read_bytes(), b"fresh")

    def test_restored_timeline_advances_even_before_new_audio_sequence(self):
        manager = SessionManager(self.audio_root)
        session, _ = manager.create("meeting-1")
        session.sequence = 0                 # server 重启后的新内存 Session
        session.audio_bytes_total = 10_000 * 32  # SQLite 恢复到 10s

        gap = session.advance_timeline_from_offset(25_000)

        self.assertEqual(gap, (10_000, 25_000))
        self.assertEqual(session.audio_bytes_total, 25_000 * 32)

    def test_active_list_and_resume_are_account_isolated(self):
        manager = SessionManager(self.audio_root)
        first, _ = manager.create("meeting-1", "TEST1")
        second, _ = manager.create("meeting-2", "TEST2")
        self.assertEqual([s["session_id"] for s in manager.active_list("TEST1")], ["meeting-1"])
        with self.assertRaises(SessionOwnershipError):
            manager.create("meeting-1", "TEST2")
        first.audio.close()
        manager.suspend(first.id, first)
        with self.assertRaises(SessionOwnershipError):
            manager.create("meeting-1", "TEST2")
        second.audio.close()
