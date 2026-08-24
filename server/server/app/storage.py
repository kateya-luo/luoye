"""会议存储：SQLite 后端（架构评审决策 2，原 JSON 文件方案已迁移）。

对外 API 形状与 JSON 时代完全一致（get_meeting/list_meetings 返回同样的 dict），
前端与其余模块零改动。新增：录制中分段逐条落库（server 崩溃不丢字幕）、
会议状态机(state)、断网洞与音频覆盖区间持久化（重启后恢复补洞）、
旧 JSON 数据一次性自动迁移（幂等，不删原文件）。
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import Database, loads_or, row_to_segment

logger = logging.getLogger("ai_recorder.storage")

SESSION_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")

_EMPTY_SUMMARY = {"summary": "暂无摘要", "decisions": [], "action_items": [],
                  "mindmap": {"title": "会议重点", "branches": []}}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _line(seg: dict[str, Any]) -> str:
    label = seg.get("speaker_label")
    return f"[{label}] {seg['text']}" if label else seg["text"]


class Storage:
    def __init__(self, root: Path):
        self.root = root
        self.db = Database(root / "clearmeeting.db")
        self._migrate_json()

    def _safe_id(self, session_id: str) -> str:
        if not SESSION_ID.fullmatch(session_id):
            raise ValueError("invalid session id")
        return session_id

    # ---------- 会议生命周期 ----------
    def create_meeting(self, session_id: str, language: str = "auto", summary_language: str = "auto",
                       owner_user_id: str = "TEST1") -> None:
        session_id = self._safe_id(session_id)
        self.db.execute(
            "INSERT OR IGNORE INTO meetings(session_id, owner_user_id, created_at, state, language, summary_language, updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (session_id, owner_user_id, _now(), "recording", language, summary_language, _now()))

    def meeting_owner(self, session_id: str) -> str | None:
        row = self.db.query_one("SELECT owner_user_id FROM meetings WHERE session_id=?",
                                (self._safe_id(session_id),))
        return row["owner_user_id"] if row else None

    def user_owns_meeting(self, session_id: str, owner_user_id: str) -> bool:
        return self.db.query_one(
            "SELECT 1 FROM meetings WHERE session_id=? AND owner_user_id=?",
            (self._safe_id(session_id), owner_user_id)) is not None

    def set_state(self, session_id: str, state: str) -> None:
        self.db.execute("UPDATE meetings SET state=?, updated_at=? WHERE session_id=?",
                        (state, _now(), self._safe_id(session_id)))

    def set_audio_end(self, session_id: str, end_ms: int) -> None:
        self.db.execute("UPDATE meetings SET audio_end_ms=?, updated_at=? WHERE session_id=?",
                        (int(end_ms), _now(), self._safe_id(session_id)))

    def set_meta_info(self, session_id: str, language: str, summary_language: str, speakers: Any) -> None:
        """延迟定稿/挂起过期前保存元数据，供重启后恢复出纪要用。"""
        self.db.execute(
            "UPDATE meetings SET language=?, summary_language=?, speakers_json=?, updated_at=? WHERE session_id=?",
            (language, summary_language, json.dumps(speakers, ensure_ascii=False), _now(),
             self._safe_id(session_id)))

    def get_meta_info(self, session_id: str) -> dict[str, Any]:
        row = self.db.query_one("SELECT language, summary_language, speakers_json FROM meetings WHERE session_id=?",
                                (self._safe_id(session_id),))
        if row is None:
            return {"language": "auto", "summary_language": "auto", "speakers": []}
        return {"language": row["language"] or "auto",
                "summary_language": row["summary_language"] or "auto",
                "speakers": loads_or(row["speakers_json"], [])}

    # ---------- 分段 ----------
    def upsert_segment(self, session_id: str, seg: dict[str, Any]) -> None:
        """录制中逐条落库：server 崩溃也不丢已定稿字幕。"""
        session_id = self._safe_id(session_id)
        self.db.execute(
            "INSERT INTO segments(session_id, seg_id, ord, start_ms, end_ms, text, speaker_id, speaker_label,"
            " speaker_final, source, state, revision,caption_revision,speaker_revision,translation_revision)"
            " VALUES(?,?,(SELECT COALESCE(MAX(ord),0)+1 FROM segments WHERE session_id=?),?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id, seg_id) DO UPDATE SET start_ms=excluded.start_ms, end_ms=excluded.end_ms,"
            " text=excluded.text, speaker_id=excluded.speaker_id, speaker_label=excluded.speaker_label,"
            " speaker_final=excluded.speaker_final, source=excluded.source, state=excluded.state,"
            " revision=excluded.revision,caption_revision=excluded.caption_revision,"
            " speaker_revision=excluded.speaker_revision,translation_revision=excluded.translation_revision",
            (session_id, seg["seg_id"], session_id, seg.get("start_ms", 0), seg.get("end_ms", 0),
             seg.get("text", ""), seg.get("speaker_id"), seg.get("speaker_label"),
             int(bool(seg.get("speaker_final"))), seg.get("source", "live"),
             seg.get("state", "provisional"), seg.get("revision", 1),
             seg.get("caption_revision", seg.get("revision", 1)),
             seg.get("speaker_revision", 0), seg.get("translation_revision", 0)))
        self.db.execute("UPDATE meetings SET updated_at=? WHERE session_id=?", (_now(), session_id))

    def apply_patch(self, session_id: str, patch: dict[str, Any]) -> None:
        """离线补洞的增量应用：删被替换的、插/改新增的。比整表重写安全（不会误删内存里没有的旧段）。"""
        session_id = self._safe_id(session_id)
        for seg_id in patch.get("removed") or []:
            self.db.execute("DELETE FROM segments WHERE session_id=? AND seg_id=?", (session_id, seg_id))
        for seg in patch.get("patches") or []:
            self.upsert_segment(session_id, seg)

    def replace_segments(self, session_id: str, segments: list[dict[str, Any]]) -> bool:
        """整表重写（meeting_end 落盘时用，此时内存 timeline 即全量）。
        译文保留：timeline 分段不携带 translation，重写前按 seg_id 捞出已落库的译文带回，防止定稿清空双语记录。"""
        session_id = self._safe_id(session_id)
        if self.db.query_one("SELECT 1 FROM meetings WHERE session_id=?", (session_id,)) is None:
            return False
        old_meta = {r["seg_id"]: dict(r) for r in self.db.query(
            "SELECT seg_id,translation,caption_revision,speaker_revision,translation_revision"
            " FROM segments WHERE session_id=?", (session_id,))}
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM segments WHERE session_id=?", (session_id,))
            for index, seg in enumerate(segments):
                seg_id = seg.get("seg_id") or f"idx-{index}"
                conn.execute(
                    "INSERT OR REPLACE INTO segments(session_id, seg_id, ord, start_ms, end_ms, text,"
                    " speaker_id,speaker_label,speaker_final,source,state,revision,translation,"
                    "caption_revision,speaker_revision,translation_revision)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, seg_id, index,
                     seg.get("start_ms", 0), seg.get("end_ms", 0), seg.get("text", ""),
                     seg.get("speaker_id"), seg.get("speaker_label"),
                     int(bool(seg.get("speaker_final"))), seg.get("source", "live"),
                     seg.get("state", "provisional"), seg.get("revision", 1),
                     seg.get("translation") or (old_meta.get(seg_id) or {}).get("translation"),
                     seg.get("caption_revision") or
                     (old_meta.get(seg_id) or {}).get("caption_revision") or seg.get("revision", 1),
                     seg.get("speaker_revision") or
                     (old_meta.get(seg_id) or {}).get("speaker_revision") or 0,
                     seg.get("translation_revision") or
                     (old_meta.get(seg_id) or {}).get("translation_revision") or 0))
            conn.execute("UPDATE meetings SET updated_at=? WHERE session_id=?", (_now(), session_id))
        return True

    def set_segment_translation(self, session_id: str, seg_id: str, translation: str) -> None:
        """实时翻译落库：译文异步后到，按 seg_id 挂回分段（双语记录的数据来源）。"""
        self.db.execute("UPDATE segments SET translation=? WHERE session_id=? AND seg_id=?",
                        (translation, self._safe_id(session_id), seg_id))

    def load_segments(self, session_id: str) -> list[dict[str, Any]]:
        rows = self.db.query("SELECT * FROM segments WHERE session_id=? ORDER BY start_ms, ord",
                             (self._safe_id(session_id),))
        return [row_to_segment(r) for r in rows]

    # ---------- 旧 API 兼容 ----------
    def save_transcript(self, session_id: str, lines: list[str], segments: list[dict[str, Any]] | None = None) -> Path:
        session_id = self._safe_id(session_id)
        self.create_meeting(session_id)
        self.replace_segments(session_id, segments or
                              [{"seg_id": f"line-{i}", "text": t} for i, t in enumerate(lines)])
        return self.root / "clearmeeting.db"

    def set_title(self, session_id: str, title: str, owner_user_id: str | None = None) -> None:
        sid = self._safe_id(session_id)
        if owner_user_id is None:
            cur = self.db.execute("UPDATE meetings SET title=?, updated_at=? WHERE session_id=?",
                                  (title[:200].strip(), _now(), sid))
        else:
            cur = self.db.execute(
                "UPDATE meetings SET title=?, updated_at=? WHERE session_id=? AND owner_user_id=?",
                (title[:200].strip(), _now(), sid, owner_user_id))
        if cur.rowcount == 0:
            raise FileNotFoundError(session_id)

    def save_summary(self, session_id: str, result: dict[str, Any]) -> Path:
        session_id = self._safe_id(session_id)
        self.create_meeting(session_id)
        self.db.execute("UPDATE meetings SET summary_json=?, state='done', updated_at=? WHERE session_id=?",
                        (json.dumps(result, ensure_ascii=False), _now(), session_id))
        return self.root / "clearmeeting.db"

    def save_summary_draft(self, session_id: str, result: dict[str, Any]) -> None:
        """只写纪要内容、不动 state——供"初稿/补录增量并入"落库（会议仍处 finalizing，
        延迟定稿状态机不受影响；最终版由 save_summary 置 done）。"""
        session_id = self._safe_id(session_id)
        self.create_meeting(session_id)
        self.db.execute("UPDATE meetings SET summary_json=?, updated_at=? WHERE session_id=?",
                        (json.dumps(result, ensure_ascii=False), _now(), session_id))

    def merge_summary(self, session_id: str, updates: dict[str, Any]) -> dict[str, Any]:
        session_id = self._safe_id(session_id)
        row = self.db.query_one("SELECT summary_json FROM meetings WHERE session_id=?", (session_id,))
        existing = loads_or(row["summary_json"] if row else None, {})
        existing.update(updates)
        self.save_summary(session_id, existing)
        return existing

    # ---------- 查询 ----------
    def list_meetings(self, owner_user_id: str | None = None) -> list[dict[str, Any]]:
        out = []
        rows = (self.db.query("SELECT * FROM meetings ORDER BY created_at DESC")
                if owner_user_id is None else self.db.query(
                    "SELECT * FROM meetings WHERE owner_user_id=? ORDER BY created_at DESC",
                    (owner_user_id,)))
        for m in rows:
            sid = m["session_id"]
            summary = loads_or(m["summary_json"], None)
            texts = [r["text"] for r in self.db.query(
                "SELECT text FROM segments WHERE session_id=? AND text != '' ORDER BY start_ms, ord LIMIT 8", (sid,))]
            count_row = self.db.query_one(
                "SELECT COUNT(*) AS n FROM segments WHERE session_id=? AND text != ''", (sid,))
            out.append({
                "session_id": sid,
                "created_at": m["created_at"],
                "title": m["title"] or (summary or {}).get("mindmap", {}).get("title") or None,
                "summary": (summary or {}).get("summary", ""),
                "transcript_preview": " ".join(texts)[:160].strip(),
                "segment_count": count_row["n"] if count_row else 0,
                "summary_pending": summary is None,
                "has_audio": self._has_audio(sid),
            })
        return out

    def get_state(self, session_id: str) -> str | None:
        row = self.db.query_one("SELECT state FROM meetings WHERE session_id=?",
                                (self._safe_id(session_id),))
        return row["state"] if row else None

    def get_meeting(self, session_id: str, owner_user_id: str | None = None) -> dict[str, Any] | None:
        session_id = self._safe_id(session_id)
        m = (self.db.query_one("SELECT * FROM meetings WHERE session_id=?", (session_id,))
             if owner_user_id is None else self.db.query_one(
                 "SELECT * FROM meetings WHERE session_id=? AND owner_user_id=?",
                 (session_id, owner_user_id)))
        if m is None:
            return None
        segments = self.load_segments(session_id)
        mark_rows = self.db.query(
            "SELECT client_mark_id,offset_samples,kind,label,created_at FROM device_session_marks"
            " WHERE server_session_id=? ORDER BY offset_samples,client_mark_id", (session_id,))
        marks = [{"id": row["client_mark_id"],
                  "at_ms": int(row["offset_samples"]) * 1000 // 16000,
                  "kind": row["kind"], "label": row["label"],
                  "created_at": row["created_at"]} for row in mark_rows]
        summary = loads_or(m["summary_json"], None)
        return {
            "session_id": session_id,
            "created_at": m["created_at"],
            "title": m["title"] or None,
            "transcript": [_line(s) for s in segments if s.get("text")],
            "segments": segments,
            "marks": marks,
            "summary": summary if summary is not None else dict(_EMPTY_SUMMARY),
            "summary_pending": summary is None,
            "has_audio": self._has_audio(session_id),
        }

    def _has_audio(self, session_id: str) -> bool:
        base = self.root / "audio_cache"
        for name in (f"{session_id}.b.pcm", f"{session_id}.pcm"):
            path = base / name
            if path.exists() and path.stat().st_size > 0:
                return True
        return False

    def cleanup_live_audio(self, session_id: str) -> None:
        """会议定稿后：通道B完整音频（.b.pcm）已就位则删实时流 .pcm，存储减半。
        .b.pcm 缺失或比 .pcm 短（如通道B中途失效）则保留 .pcm 兜底。"""
        base = self.root / "audio_cache"
        b_path = base / f"{session_id}.b.pcm"
        p_path = base / f"{session_id}.pcm"
        try:
            if (b_path.exists() and p_path.exists()
                    and b_path.stat().st_size >= p_path.stat().st_size):
                p_path.unlink()
                logger.info("live_audio_cleaned session_id=%s (playback uses .b.pcm)", session_id)
        except OSError:
            pass

    def delete_meeting(self, session_id: str, owner_user_id: str | None = None) -> bool:
        session_id = self._safe_id(session_id)
        existed = (self.db.query_one("SELECT 1 FROM meetings WHERE session_id=?", (session_id,))
                   if owner_user_id is None else self.db.query_one(
                       "SELECT 1 FROM meetings WHERE session_id=? AND owner_user_id=?",
                       (session_id, owner_user_id))) is not None
        if not existed:
            return False
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM meetings WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM segments WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM coverage WHERE session_id=?", (session_id,))
            conn.execute("DELETE FROM pending_gaps WHERE session_id=?", (session_id,))
        for path in [self.root / "audio_cache" / f"{session_id}.pcm",
                     self.root / "audio_cache" / f"{session_id}.b.pcm",
                     self.root / "audio_cache" / f"{session_id}.wav",
                     self.root / "transcripts" / f"{session_id}.json",
                     self.root / "summaries" / f"{session_id}.json",
                     self.root / "summaries" / f"{session_id}.md"]:
            existed = path.exists() or existed
            path.unlink(missing_ok=True)
        return existed

    # ---------- 断网洞 / 覆盖区间（重启恢复）----------
    def add_gap(self, session_id: str, start_ms: int, end_ms: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO pending_gaps(session_id, start_ms, end_ms) VALUES(?,?,?)",
                        (self._safe_id(session_id), int(start_ms), int(end_ms)))

    def delete_gap(self, session_id: str, start_ms: int) -> None:
        self.db.execute("DELETE FROM pending_gaps WHERE session_id=? AND start_ms=?",
                        (self._safe_id(session_id), int(start_ms)))

    def list_gaps(self, session_id: str | None = None) -> list[tuple[str, int, int]]:
        if session_id:
            rows = self.db.query("SELECT * FROM pending_gaps WHERE session_id=?", (self._safe_id(session_id),))
        else:
            rows = self.db.query("SELECT * FROM pending_gaps")
        return [(r["session_id"], r["start_ms"], r["end_ms"]) for r in rows]

    def save_coverage(self, session_id: str, intervals: list[tuple[int, int]]) -> None:
        session_id = self._safe_id(session_id)
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM coverage WHERE session_id=?", (session_id,))
            conn.executemany("INSERT INTO coverage(session_id, start_ms, end_ms) VALUES(?,?,?)",
                             [(session_id, s, e) for s, e in intervals])

    def load_all_coverage(self) -> dict[str, list[tuple[int, int]]]:
        out: dict[str, list[tuple[int, int]]] = {}
        for r in self.db.query("SELECT * FROM coverage ORDER BY session_id, start_ms"):
            out.setdefault(r["session_id"], []).append((r["start_ms"], r["end_ms"]))
        return out

    def unfinished_meetings(self, older_than_seconds: float = 300) -> list[dict[str, Any]]:
        """重启恢复扫描：非 done、且一段时间没动静的会议（避开正在直播/刚重连的）。"""
        cutoff = (datetime.now(timezone.utc) - timedelta(seconds=older_than_seconds)).isoformat()
        rows = self.db.query(
            "SELECT m.session_id, m.state, m.audio_end_ms FROM meetings m"
            " WHERE m.state != 'done' AND (m.updated_at IS NULL OR m.updated_at < ?)"
            " AND NOT EXISTS ("
            "   SELECT 1 FROM device_sessions d"
            "   WHERE d.server_session_id=m.session_id AND d.status='awaiting_repair'"
            " )", (cutoff,))
        return [dict(r) for r in rows]

    # ---------- 导出 ----------
    def export_meeting(self, session_id: str, format: str,
                       owner_user_id: str | None = None) -> tuple[str, str, str] | None:
        meeting = self.get_meeting(session_id, owner_user_id)
        if meeting is None:
            return None
        if format == "json":
            return json.dumps(meeting, ensure_ascii=False, indent=2), "application/json; charset=utf-8", "json"
        if format == "txt":
            return self._as_text(meeting), "text/plain; charset=utf-8", "txt"
        return self._as_markdown(meeting), "text/markdown; charset=utf-8", "md"

    @staticmethod
    def _as_text(meeting: dict[str, Any]) -> str:
        summary = meeting["summary"]
        decisions = "\n".join(f"- {item}" for item in summary["decisions"]) or "- 无"
        actions = "\n".join(
            f"- {item.get('task', '待确认')}（负责人：{item.get('assignee', '待确认')}；截止：{item.get('deadline', '待确认')}）"
            for item in summary["action_items"] if isinstance(item, dict)
        ) or "- 无"
        transcript = "\n".join(meeting["transcript"])
        mindmap = summary.get("mindmap") or {}
        mindmap_lines = []
        for branch in mindmap.get("branches", []):
            if isinstance(branch, dict):
                mindmap_lines.append(f"- {branch.get('title', '重点')}：" + "；".join(str(item) for item in branch.get("items", [])))
        mindmap_text = "\n".join(mindmap_lines) or "- 无"
        return f"会议时间：{meeting['created_at']}\n\n摘要\n{summary['summary']}\n\n决策\n{decisions}\n\n待办事项\n{actions}\n\n思维导图\n{mindmap_text}\n\n完整转写\n{transcript}\n"

    @classmethod
    def _as_markdown(cls, meeting: dict[str, Any]) -> str:
        return "# 会议纪要\n\n" + cls._as_text(meeting).replace("摘要\n", "## 摘要\n", 1).replace("\n决策\n", "\n## 决策\n", 1).replace("\n待办事项\n", "\n## 待办事项\n", 1).replace("\n思维导图\n", "\n## 思维导图\n", 1).replace("\n完整转写\n", "\n## 完整转写\n", 1)

    # ---------- 旧 JSON 一次性迁移 ----------
    def _migrate_json(self) -> None:
        if self.db.get_meta("json_migrated") == "1":
            return
        transcript_dir = self.root / "transcripts"
        migrated = 0
        for path in sorted(transcript_dir.glob("*.json")) if transcript_dir.exists() else []:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                sid = data.get("session_id") or path.stem
                if not SESSION_ID.fullmatch(sid):
                    continue
                if self.db.query_one("SELECT 1 FROM meetings WHERE session_id=?", (sid,)):
                    continue
                created = data.get("created_at") or datetime.fromtimestamp(
                    path.stat().st_mtime, timezone.utc).isoformat()
                summary = self._read_legacy_summary(sid)
                self.db.execute(
                    "INSERT INTO meetings(session_id, created_at, title, state, summary_json, updated_at)"
                    " VALUES(?,?,?,?,?,?)",
                    (sid, created, data.get("title"), "done",
                     json.dumps(summary, ensure_ascii=False) if summary else None, _now()))
                segments = data.get("segments") or []
                if not segments:
                    segments = [{"seg_id": f"line-{i}", "text": t} for i, t in enumerate(data.get("transcript") or [])]
                self.replace_segments(sid, [
                    {**seg, "seg_id": seg.get("seg_id") or f"mig-{i}"} for i, seg in enumerate(segments)])
                migrated += 1
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                logger.warning("json_migration_skipped file=%s error=%s", path.name, exc)
        self.db.set_meta("json_migrated", "1")
        if migrated:
            logger.info("json_migration_done meetings=%d (原 JSON 文件保留作为备份)", migrated)

    def _read_legacy_summary(self, session_id: str) -> dict[str, Any] | None:
        json_path = self.root / "summaries" / f"{session_id}.json"
        if json_path.exists():
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    return payload
            except (OSError, json.JSONDecodeError):
                pass
        md_path = self.root / "summaries" / f"{session_id}.md"
        if md_path.exists():
            return {**_EMPTY_SUMMARY, "summary": md_path.read_text(encoding="utf-8")}
        return None
