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

    def set_speaker_diarization(self, session_id: str, enabled: bool) -> None:
        self.db.execute(
            "UPDATE meetings SET speaker_diarization_enabled=?,updated_at=? WHERE session_id=?",
            (int(bool(enabled)), _now(), self._safe_id(session_id)))

    def needs_canonical_finalization(self, session_id: str) -> bool:
        """Whether a completed browser recording has authoritative PCM ready for canonical ASR."""
        row = self.db.query_one(
            "SELECT m.audio_end_ms FROM meetings m WHERE m.session_id=?"
            " AND NOT EXISTS (SELECT 1 FROM device_sessions d WHERE d.server_session_id=m.session_id)",
            (self._safe_id(session_id),))
        if row is None or int(row["audio_end_ms"] or 0) <= 0:
            return False
        path = self.root / "audio_cache" / f"{session_id}.b.pcm"
        expected_bytes = int(row["audio_end_ms"] or 0) * 32
        return path.exists() and path.stat().st_size >= expected_bytes

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
            " speaker_final, source, state, revision)"
            " VALUES(?,?,(SELECT COALESCE(MAX(ord),0)+1 FROM segments WHERE session_id=?),?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(session_id, seg_id) DO UPDATE SET start_ms=excluded.start_ms, end_ms=excluded.end_ms,"
            " text=excluded.text, speaker_id=excluded.speaker_id, speaker_label=excluded.speaker_label,"
            " speaker_final=excluded.speaker_final, source=excluded.source, state=excluded.state,"
            " revision=excluded.revision",
            (session_id, seg["seg_id"], session_id, seg.get("start_ms", 0), seg.get("end_ms", 0),
             seg.get("text", ""), seg.get("speaker_id"), seg.get("speaker_label"),
             int(bool(seg.get("speaker_final"))), seg.get("source", "live"),
             seg.get("state", "provisional"), seg.get("revision", 1)))
        self.db.execute("UPDATE meetings SET updated_at=? WHERE session_id=?", (_now(), session_id))
        self.db.execute(
            "UPDATE meetings SET transcript_revision=transcript_revision+1,minutes_status="
            "CASE WHEN minutes_status='ready' THEN 'outdated' ELSE minutes_status END WHERE session_id=?",
            (session_id,))

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
        old_tr = {r["seg_id"]: r["translation"] for r in self.db.query(
            "SELECT seg_id, translation FROM segments WHERE session_id=? AND translation IS NOT NULL",
            (session_id,))}
        with self.db.transaction() as conn:
            conn.execute("DELETE FROM segments WHERE session_id=?", (session_id,))
            for index, seg in enumerate(segments):
                seg_id = seg.get("seg_id") or f"idx-{index}"
                conn.execute(
                    "INSERT OR REPLACE INTO segments(session_id, seg_id, ord, start_ms, end_ms, text,"
                    " speaker_id, speaker_label, speaker_final, source, state, revision, translation)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, seg_id, index,
                     seg.get("start_ms", 0), seg.get("end_ms", 0), seg.get("text", ""),
                     seg.get("speaker_id"), seg.get("speaker_label"),
                     int(bool(seg.get("speaker_final"))), seg.get("source", "live"),
                     seg.get("state", "provisional"), seg.get("revision", 1),
                     seg.get("translation") or old_tr.get(seg_id)))
            conn.execute(
                "UPDATE meetings SET transcript_revision=transcript_revision+1,minutes_status="
                "CASE WHEN minutes_status='ready' THEN 'outdated' ELSE minutes_status END,updated_at=?"
                " WHERE session_id=?", (_now(), session_id))
        return True

    def replace_canonical_segments(
        self, session_id: str, segments: list[dict[str, Any]], run: dict[str, Any]
    ) -> int:
        """Atomically publish a verified whole-meeting ASR+diarization result."""
        session_id = self._safe_id(session_id)
        if self.db.query_one("SELECT 1 FROM meetings WHERE session_id=?", (session_id,)) is None:
            raise FileNotFoundError(session_id)
        old_tr = {r["seg_id"]: r["translation"] for r in self.db.query(
            "SELECT seg_id,translation FROM segments WHERE session_id=?"
            " AND translation IS NOT NULL", (session_id,))}
        now = _now()
        with self.db.transaction() as conn:
            row = conn.execute(
                "SELECT revision FROM device_sessions WHERE server_session_id=?",
                (session_id,)).fetchone()
            revision = int(row["revision"] if row is not None else 0) + 1
            conn.execute("DELETE FROM segments WHERE session_id=?", (session_id,))
            for index, seg in enumerate(segments):
                seg_id = str(seg.get("seg_id") or f"canonical-{index}")
                conn.execute(
                    "INSERT INTO segments(session_id,seg_id,ord,start_ms,end_ms,text,"
                    "speaker_id,speaker_label,speaker_final,source,state,revision,translation)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (session_id, seg_id, index, int(seg.get("start_ms") or 0),
                     int(seg.get("end_ms") or 0), str(seg.get("text") or ""),
                     seg.get("speaker_id"), seg.get("speaker_label"), 1,
                     "offline_canonical", "final", revision,
                     seg.get("translation") or old_tr.get(seg_id)))
            conn.execute(
                "UPDATE meetings SET speakers_json=?,transcript_revision=transcript_revision+1,"
                "minutes_status=CASE WHEN minutes_status='ready' THEN 'outdated' ELSE minutes_status END,"
                "updated_at=? WHERE session_id=?",
                (json.dumps(run.get("speakers") or [], ensure_ascii=False), now, session_id))
            if row is not None:
                conn.execute(
                    "UPDATE device_sessions SET revision=?,updated_at=?"
                    " WHERE server_session_id=?", (revision, now, session_id))
            run_table = "canonical_diarization_runs" if row is not None else "meeting_canonical_runs"
            conn.execute(
                f"INSERT INTO {run_table}(session_id,canonical_sha256,"
                "pipeline_version,state,segment_count,speaker_count,processing_ms,"
                "realtime_factor,last_error,created_at,updated_at)"
                " VALUES(?,?,?,'done',?,?,?,?,NULL,?,?)"
                " ON CONFLICT(session_id) DO UPDATE SET canonical_sha256=excluded.canonical_sha256,"
                "pipeline_version=excluded.pipeline_version,state='done',"
                "segment_count=excluded.segment_count,speaker_count=excluded.speaker_count,"
                "processing_ms=excluded.processing_ms,realtime_factor=excluded.realtime_factor,"
                "last_error=NULL,updated_at=excluded.updated_at",
                (session_id, str(run.get("canonical_sha256") or ""),
                 str(run.get("pipeline_version") or ""), len(segments),
                 int(run.get("speaker_count") or 0), int(run.get("processing_ms") or 0),
                 float(run.get("realtime_factor") or 0), now, now))
        return revision

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
        self.db.execute("UPDATE meetings SET summary_json=?, state='done',minutes_status='ready', updated_at=? WHERE session_id=?",
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
    def get_processing_status(self, session_id: str) -> dict[str, Any] | None:
        """Return user-facing, evidence-based background transcription progress.

        FunASR processes one slice as a single blocking request, so the server cannot
        honestly report a continuously increasing percentage *inside* that request.
        This status therefore exposes durable upload coverage, queue/job transitions,
        the canonical diarization barrier and an ETA derived from completed canonical
        runs.  Clients can poll this cheaply without reading transcript bodies.
        """
        session_id = self._safe_id(session_id)
        meeting = self.db.query_one(
            "SELECT state,audio_end_ms,updated_at FROM meetings WHERE session_id=?",
            (session_id,))
        if meeting is None:
            return None
        device = self.db.query_one(
            "SELECT status,canonical_total_bytes,expected_samples,sample_rate,channels,bits_per_sample,"
            "failure_code,failure_message,updated_at FROM device_sessions"
            " WHERE server_session_id=?", (session_id,))
        jobs = self.db.query(
            "SELECT id,start_ms,end_ms,reason,state,attempts,order_key,last_error,"
            "created_at,updated_at FROM offline_asr_jobs WHERE session_id=?"
            " ORDER BY order_key,chunk_index,id", (session_id,))

        active_jobs = [row for row in jobs if row["state"] in {"queued", "running"}]
        failed_jobs = [row for row in jobs if row["state"] == "failed"]
        device_status = str(device["status"] or "") if device else ""
        meeting_state = str(meeting["state"] or "")

        # The common completed path stays deliberately compact; list_meetings calls
        # this method for every row, while only active rows need the heavier metrics.
        if (not active_jobs and not failed_jobs and meeting_state in {"done", "transcript_ready"}
                and device_status not in {"uploading", "awaiting_repair", "processing", "failed"}):
            return {
                "active": False, "stage": "completed", "progress_percent": 100,
                "title": "后台转录已完成", "detail": "完整文字和多人识别结果已经写回。",
                "updated_at": meeting["updated_at"],
            }

        expected_samples = int(device["expected_samples"] or 0) if device else 0
        sample_rate = max(1, int(device["sample_rate"] or 16000)) if device else 16000
        audio_duration_ms = (expected_samples * 1000 // sample_rate) if expected_samples else int(
            meeting["audio_end_ms"] or 0)
        if audio_duration_ms <= 0:
            audio_duration_ms = max(
                [int(row["end_ms"] or 0) for row in jobs] + [0])

        total_bytes = int(device["canonical_total_bytes"] or 0) if device else 0
        # Count the union of durable byte ranges.  A sparse .part file may have the
        # final expected size even while its middle is missing, and duplicate or
        # overlapping retries must not inflate the displayed upload percentage.
        range_rows = self.db.query(
            "SELECT start_byte,end_byte FROM device_audio_ranges"
            " WHERE server_session_id=? ORDER BY start_byte,end_byte",
            (session_id,)) if device else []
        merged_ranges: list[list[int]] = []
        for row in range_rows:
            start = max(0, int(row["start_byte"] or 0))
            end = max(start, int(row["end_byte"] or 0))
            if total_bytes:
                start, end = min(start, total_bytes), min(end, total_bytes)
            if end <= start:
                continue
            if merged_ranges and start <= merged_ranges[-1][1]:
                merged_ranges[-1][1] = max(merged_ranges[-1][1], end)
            else:
                merged_ranges.append([start, end])
        received_bytes = sum(end - start for start, end in merged_ranges)
        missing_bytes = max(0, total_bytes - received_bytes) if total_bytes else 0
        bytes_per_second = 0
        if device:
            bytes_per_second = (sample_rate * max(1, int(device["channels"] or 1))
                                * max(8, int(device["bits_per_sample"] or 16)) // 8)
        missing_duration_ms = (round(missing_bytes * 1000 / bytes_per_second)
                               if bytes_per_second else None)
        upload_percent = (min(100, round(received_bytes * 100 / total_bytes))
                          if total_bytes else (100 if device_status in {"processing", "done"} else None))

        work_jobs = [row for row in jobs if row["reason"] not in {"publish", "summarize"}]
        counts = {state: sum(1 for row in work_jobs if row["state"] == state)
                  for state in ("queued", "running", "done", "failed", "cancelled")}
        canonical = next((row for row in work_jobs if row["reason"] == "canonical"), None)
        noncanonical_active = [row for row in active_jobs
                               if row["reason"] not in {"canonical", "publish", "summarize"}]
        publish_active = next((row for row in active_jobs
                               if row["reason"] in {"publish", "summarize"}), None)

        if device_status == "failed" or failed_jobs:
            code = (str(device["failure_code"] or "") if device else "") or "OFFLINE_TRANSCRIPTION_FAILED"
            return {
                "active": False, "stage": "failed", "progress_percent": None,
                "title": "后台转录未完成",
                "detail": "自动重试已经停止，原始录音仍然保留，可检查服务后重新处理。",
                "error_code": code, "updated_at": (device["updated_at"] if device else meeting["updated_at"]),
                "jobs": {**counts, "total": len(work_jobs)},
            }

        upload_incomplete = bool(total_bytes
                                 and device_status in {"uploading", "awaiting_repair"})

        if (meeting_state in {"recording", "suspended"} and not active_jobs
                and not upload_incomplete):
            return {
                "active": False, "stage": "recording", "progress_percent": None,
                "title": "会议仍在录制", "detail": "会议结束或离线片段到齐后会自动开始后台整理。",
                "updated_at": meeting["updated_at"],
            }

        if (not active_jobs and not upload_incomplete
                and (meeting_state == "finalizing" or device_status == "processing")):
            try:
                changed = datetime.fromisoformat(str(
                    (device["updated_at"] if device else None) or meeting["updated_at"] or ""))
                if changed.tzinfo is None:
                    changed = changed.replace(tzinfo=timezone.utc)
                stalled_seconds = (datetime.now(timezone.utc) - changed).total_seconds()
            except (TypeError, ValueError):
                stalled_seconds = 0
            if stalled_seconds >= 300:
                return {
                    "active": False, "stage": "stalled", "progress_percent": None,
                    "title": "后台处理已停滞",
                    "detail": "服务器超过 5 分钟没有找到可执行任务，原始录音仍然保留；这不是正常等待状态，需要重新提交或检查队列。",
                    "error_code": "BACKGROUND_QUEUE_STALLED",
                    "stalled_seconds": int(stalled_seconds),
                    "updated_at": (device["updated_at"] if device else meeting["updated_at"]),
                    "jobs": {**counts, "total": len(work_jobs)},
                }

        # Historical real-time factor makes the ETA adapt to this server instead of
        # embedding a hardware-specific constant.  A conservative default is used on
        # the first run and the range shown by clients remains intentionally broad.
        factors = []
        for table in ("canonical_diarization_runs", "meeting_canonical_runs"):
            rows = self.db.query(
                f"SELECT realtime_factor FROM {table} WHERE state='done'"
                " AND realtime_factor>0 ORDER BY updated_at DESC LIMIT 20")
            factors.extend(float(row["realtime_factor"]) for row in rows)
        realtime_factor = (sum(factors) / len(factors)) if factors else 0.35
        realtime_factor = max(0.05, min(2.0, realtime_factor))
        workers = 4

        def remaining_seconds(row: Any) -> float:
            duration_ms = (audio_duration_ms if row["reason"] == "canonical" else
                           max(0, int(row["end_ms"] or 0) - int(row["start_ms"] or 0)))
            estimate = max(5.0, duration_ms / 1000 * realtime_factor)
            if row["state"] == "running" and row["updated_at"]:
                try:
                    started = datetime.fromisoformat(str(row["updated_at"]))
                    if started.tzinfo is None:
                        started = started.replace(tzinfo=timezone.utc)
                    estimate -= max(0.0, (datetime.now(timezone.utc) - started).total_seconds())
                except (TypeError, ValueError):
                    pass
            return max(3.0, estimate)

        slice_remaining = [remaining_seconds(row) for row in noncanonical_active]
        slice_eta = (max(max(slice_remaining), sum(slice_remaining) / workers)
                     if slice_remaining else 0.0)
        canonical_eta = (remaining_seconds(canonical)
                         if canonical is not None and canonical["state"] in {"queued", "running"}
                         else 0.0)
        eta_seconds = int(round(slice_eta + canonical_eta + (5 if publish_active else 0)))

        queue_ahead = 0
        if active_jobs:
            first_order = min(str(row["order_key"] or "") for row in active_jobs)
            ahead = self.db.query_one(
                "SELECT COUNT(*) AS n FROM offline_asr_jobs WHERE session_id<>?"
                " AND state IN ('queued','running') AND order_key<=?",
                (session_id, first_order))
            queue_ahead = int(ahead["n"] or 0) if ahead else 0

        # Job-weighted stage progress.  This moves only at trustworthy boundaries;
        # during one FunASR call the UI explicitly says it is processing that slice.
        total_work_ms = 0
        done_work_ms = 0
        for row in work_jobs:
            duration = (audio_duration_ms if row["reason"] == "canonical" else
                        max(1, int(row["end_ms"] or 0) - int(row["start_ms"] or 0)))
            total_work_ms += duration
            if row["state"] == "done":
                done_work_ms += duration
        work_fraction = done_work_ms / total_work_ms if total_work_ms else 0.0

        if upload_incomplete:
            stage = "waiting_upload" if device_status == "awaiting_repair" else "uploading"
            # During upload this bar describes upload coverage, not a synthetic
            # whole-pipeline score.  Showing 69% as 24% (69% * 35%) made a completed
            # device send look almost empty and hid the actionable missing audio.
            progress = upload_percent or 0
            title = ("等待设备补传缺失音频" if stage == "waiting_upload"
                     else "离线音频正在上传")
            detail = f"服务器已确认接收 {upload_percent or 0}% 的录音"
            if missing_duration_ms:
                missing_minutes, missing_seconds = divmod(round(missing_duration_ms / 1000), 60)
                if missing_minutes:
                    detail += f"，仍缺约 {missing_minutes} 分 {missing_seconds} 秒"
                else:
                    detail += f"，仍缺约 {missing_seconds} 秒"
            if counts["done"]:
                detail += f"；已收到的部分完成了 {counts['done']} 个转写片段"
            detail += "。"
        elif noncanonical_active:
            stage = "transcribing" if any(row["state"] == "running" for row in noncanonical_active) else "queued"
            progress = max(36, min(78, round(35 + work_fraction * 55)))
            title = "正在转写离线录音" if stage == "transcribing" else "离线录音已排队"
            detail = (f"后台任务已完成 {counts['done']}/{len(work_jobs)} 个；"
                      + ("当前正在处理音频片段。" if stage == "transcribing" else "正在等待处理核心。"))
        elif canonical is not None and canonical["state"] in {"queued", "running"}:
            stage = "diarizing" if canonical["state"] == "running" else "diarization_queued"
            progress = max(80, min(94, round(35 + work_fraction * 55)))
            title = "正在统一转写与多人识别" if stage == "diarizing" else "多人识别已排队"
            detail = "离线片段已经转完，正在用整场录音校正文字并统一说话人。"
        elif publish_active or meeting_state == "finalizing" or device_status == "processing":
            stage = "publishing"
            progress = 96
            title = "正在写回完整转写"
            detail = "后台识别已经结束，正在原子更新会议文字和人员结果。"
            eta_seconds = max(3, eta_seconds)
        else:
            stage = "completed"
            progress = 100
            title = "后台转录已完成"
            detail = "完整文字和多人识别结果已经写回。"

        retrying = any(int(row["attempts"] or 0) > 0 and row["state"] == "queued"
                       for row in active_jobs)
        if retrying and stage not in {"uploading", "completed"}:
            detail += " 上一次处理未成功，服务器正在自动重试。"
        updated_values = [str(row["updated_at"]) for row in active_jobs if row["updated_at"]]
        updated_values.extend([str(meeting["updated_at"] or "")])
        return {
            "active": stage not in {"completed", "failed"},
            "stage": stage,
            "progress_percent": progress,
            "title": title,
            "detail": detail,
            "audio_duration_ms": audio_duration_ms,
            "upload": {"received_bytes": received_bytes, "total_bytes": total_bytes,
                       "missing_bytes": missing_bytes,
                       "missing_duration_ms": missing_duration_ms,
                       "percent": upload_percent},
            "jobs": {**counts, "total": len(work_jobs)},
            "queue_ahead_jobs": queue_ahead,
            "eta_seconds": eta_seconds if eta_seconds > 0 else None,
            "eta_lower_seconds": max(10, round(eta_seconds * 0.7)) if eta_seconds > 0 else None,
            "eta_upper_seconds": max(30, round(eta_seconds * 1.5)) if eta_seconds > 0 else None,
            "estimate_basis": "historical_realtime_factor" if factors else "conservative_default",
            "updated_at": max(updated_values) if updated_values else meeting["updated_at"],
        }

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
                "state": m["state"],
                "minutes_status": m["minutes_status"] if "minutes_status" in m.keys() else ("ready" if summary else "not_created"),
                "summary_pending": (m["minutes_status"] in {"queued", "generating"}) if "minutes_status" in m.keys() else False,
                "has_audio": self._has_audio(sid),
                "processing": self.get_processing_status(sid),
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
            "summary": summary if summary is not None else {},
            "state": m["state"],
            "minutes_status": m["minutes_status"] if "minutes_status" in m.keys() else ("ready" if summary else "not_created"),
            "transcript_revision": int(m["transcript_revision"] or 0) if "transcript_revision" in m.keys() else 0,
            "speaker_revision": int(m["speaker_revision"] or 0) if "speaker_revision" in m.keys() else 0,
            "summary_pending": (m["minutes_status"] in {"queued", "generating"}) if "minutes_status" in m.keys() else False,
            "has_audio": self._has_audio(session_id),
            "processing": self.get_processing_status(session_id),
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
            now = _now()
            conn.execute(
                "UPDATE offline_asr_jobs SET state='cancelled',lease_owner=NULL,lease_until=NULL,"
                "updated_at=? WHERE session_id=? AND state IN ('queued','running')",
                (now, session_id))
            conn.execute(
                "UPDATE device_sessions SET status='cancelled',revision=revision+1,updated_at=?"
                " WHERE server_session_id=? AND status NOT IN ('done','failed','cancelled')",
                (now, session_id))
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
        transcript = "\n".join(meeting["transcript"])
        if not summary:
            return f"会议时间：{meeting['created_at']}\n\n完整转写\n{transcript}\n"
        decisions = "\n".join(f"- {item}" for item in summary["decisions"]) or "- 无"
        actions = "\n".join(
            f"- {item.get('task', '待确认')}（负责人：{item.get('assignee', '待确认')}；截止：{item.get('deadline', '待确认')}）"
            for item in summary["action_items"] if isinstance(item, dict)
        ) or "- 无"
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
