"""离线转写任务队列 + 补洞合并。

网页录音的旧链路仍可使用进程内 FIFO；设备录音传入 SQLite 后使用持久队列、并行
worker、失败退避和按会话完成屏障。这样两小时录音可以边上传边转写，服务重启也不会
丢任务或提前生成纪要。
"""
from __future__ import annotations

import asyncio
import logging
import os
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

from .funasr_offline_client import FunASROfflineClient
from .segments import Patch, Timeline

logger = logging.getLogger("ai_recorder.offline_jobs")

JobReason = Literal["gap", "bulk", "finalize", "canonical", "publish", "summarize"]
BYTES_PER_MS = 16000 * 2 / 1000


@dataclass
class OfflineJob:
    session_id: str
    start_ms: int
    end_ms: int
    reason: JobReason
    attempts: int = 0
    job_id: int | None = None


class OfflineJobQueue:
    def __init__(
        self,
        audio_root: Path,
        get_timeline: Callable[[str], Timeline | None],
        on_applied: Callable[[str, Timeline, Patch, str], Awaitable[None]],
        offline: FunASROfflineClient | None = None,
        on_summarize: Callable[[str], Awaitable[None]] | None = None,
        on_canonical: Callable[[str, dict[str, Any]], Awaitable[None]] | None = None,
        on_gap_done: Callable[[str, int, int], None] | None = None,
        on_give_up: Callable[[OfflineJob, Exception], Awaitable[None]] | None = None,
        db: Any | None = None,
        worker_count: int | None = None,
    ) -> None:
        self.audio_root = audio_root
        self.get_timeline = get_timeline            # session_id -> Timeline | None
        self.on_applied = on_applied                # (session_id, timeline, patch, reason) -> 持久化+广播+重算
        self.on_summarize = on_summarize            # (session_id) -> 会议最终纪要（哨兵任务，在补洞之后执行一次）
        self.on_canonical = on_canonical            # 整场权威ASR+声纹结果原子提交
        self.on_gap_done = on_gap_done              # (session_id, start_ms, end_ms) -> 洞处理完成（删持久化记录）
        self.on_give_up = on_give_up                # retries exhausted -> durable terminal state
        self.offline = offline or FunASROfflineClient()
        self.db = db
        self._queue: asyncio.Queue[OfflineJob] = asyncio.Queue()
        self._inflight: set[tuple[str, int, int, str]] = set()  # 去重，避免同区间重复入队
        self._worker: asyncio.Task | None = None
        self._workers: list[asyncio.Task] = []
        self._wake = asyncio.Event()
        self._claim_lock = asyncio.Lock()
        self.worker_count = max(1, min(8, int(
            worker_count if worker_count is not None else os.getenv("OFFLINE_ASR_WORKERS", "4"))))
        self.lease_seconds = max(300, int(os.getenv("OFFLINE_ASR_LEASE_SECONDS", "7200")))
        # 首次启动 CPU FunASR 可能需要加载/下载模型数分钟。持久任务不应因为依赖服务
        # 尚未 ready 就在四五分钟内进入永久失败。
        self.max_retries = max(1, int(os.getenv("OFFLINE_ASR_MAX_RETRIES", "30")))
        self.retry_base_seconds = max(1, int(os.getenv("OFFLINE_ASR_RETRY_BASE_SECONDS", "5")))

    def busy(self, session_id: str) -> bool:
        """该会议是否还有排队/在跑的任务（含哨兵）。用于 meeting_end 判断能否内联出最终纪要。"""
        if self.db is not None:
            row = self.db.query_one(
                "SELECT 1 FROM offline_asr_jobs WHERE session_id=? AND state IN ('queued','running') LIMIT 1",
                (session_id,))
            return row is not None
        return any(key[0] == session_id for key in self._inflight)

    def progress(self, session_id: str) -> dict[str, Any]:
        if self.db is None:
            return {"queued": 0, "running": int(self.busy(session_id)), "done": 0,
                    "failed": 0, "sealed": False}
        rows = self.db.query(
            "SELECT state,reason,COUNT(*) AS n FROM offline_asr_jobs WHERE session_id=?"
            " GROUP BY state,reason", (session_id,))
        counts = {"queued": 0, "running": 0, "done": 0, "failed": 0, "cancelled": 0}
        sealed = False
        finalization = "open"
        for row in rows:
            state, reason, count = str(row["state"]), str(row["reason"]), int(row["n"])
            if reason in {"summarize", "publish"}:
                sealed = True
                finalization = state
                continue
            if state in counts:
                counts[state] += count
        total = sum(counts.values())
        return {**counts, "total": total, "sealed": sealed,
                "finalization": finalization, "workers": self.worker_count}

    def start(self) -> None:
        if self.db is not None:
            if self._workers:
                return
            now = self._now()
            # 单实例部署：进程重启意味着旧 lease 的 owner 已消失，立即恢复即可。
            self.db.execute(
                "UPDATE offline_asr_jobs SET state='queued',lease_owner=NULL,lease_until=NULL,"
                "available_at=?,updated_at=? WHERE state='running'", (now, now))
            self._workers = [asyncio.create_task(self._run_persistent(index))
                             for index in range(self.worker_count)]
            self._wake.set()
            logger.info("offline_persistent_workers_started count=%d", self.worker_count)
            return
        if self._worker is None:
            self._worker = asyncio.create_task(self._run())
            logger.info("offline_worker_started")

    async def enqueue(self, session_id: str, start_ms: int, end_ms: int, reason: JobReason,
                      *, order_key: str | None = None, chunk_index: int | None = None) -> None:
        if self.db is not None:
            now = self._now()
            self.db.execute(
                "INSERT OR IGNORE INTO offline_asr_jobs(session_id,start_ms,end_ms,reason,"
                "chunk_index,order_key,state,attempts,available_at,created_at,updated_at)"
                " VALUES(?,?,?,?,?,?,'queued',0,?,?,?)",
                (session_id, int(start_ms), int(end_ms), reason,
                 int(chunk_index if chunk_index is not None else 0), order_key or now,
                 now, now, now))
            self._wake.set()
            logger.info("offline_job_persisted session_id=%s range=[%d,%d) reason=%s",
                        session_id, start_ms, end_ms, reason)
            return
        key = (session_id, int(start_ms), int(end_ms), reason)
        if key in self._inflight:
            return
        self._inflight.add(key)
        await self._queue.put(OfflineJob(session_id, int(start_ms), int(end_ms), reason))
        logger.info("offline_job_queued session_id=%s range=[%d,%d) reason=%s", session_id, start_ms, end_ms, reason)

    async def _run(self) -> None:
        while True:
            job = await self._queue.get()
            key = (job.session_id, job.start_ms, job.end_ms, job.reason)
            try:
                # 在当前 worker 内重试，不能把失败任务延迟放回队尾；否则后面的 summarize
                # 会越过它，破坏“补洞全部完成后再定稿”的顺序保证。
                while True:
                    try:
                        await self._process(job)
                        break
                    except Exception as exc:
                        job.attempts += 1
                        if job.attempts > self.max_retries:
                            logger.exception("offline_job_gave_up session_id=%s reason=%s error=%s",
                                             job.session_id, job.reason, exc)
                            if self.on_give_up is not None:
                                try:
                                    await self.on_give_up(job, exc)
                                except Exception:
                                    logger.exception(
                                        "offline_job_give_up_callback_failed session_id=%s reason=%s",
                                        job.session_id, job.reason)
                            break
                        logger.warning("offline_job_retry session_id=%s reason=%s attempt=%d/%d error=%s",
                                       job.session_id, job.reason, job.attempts, self.max_retries, exc)
                        await asyncio.sleep(min(120, self.retry_base_seconds * (2 ** (job.attempts - 1))))
                self._inflight.discard(key)
            except asyncio.CancelledError:
                raise
            finally:
                self._queue.task_done()

    async def stop(self) -> None:
        """测试/优雅关闭使用：停止 worker，不遗留后台任务。"""
        if self._workers:
            for worker in self._workers:
                worker.cancel()
            for worker in self._workers:
                with suppress(asyncio.CancelledError):
                    await worker
            self._workers.clear()
            return
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    async def _run_persistent(self, worker_index: int) -> None:
        owner = f"{os.getpid()}-{worker_index}-{uuid.uuid4().hex[:8]}"
        while True:
            job = await self._claim(owner)
            if job is None:
                self._wake.clear()
                # 清事件与查询之间可能正好入队，再查一次避免丢唤醒。
                job = await self._claim(owner)
                if job is None:
                    try:
                        await asyncio.wait_for(self._wake.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
                    continue
            try:
                await self._process(job)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._persistent_failure(job, exc)
            else:
                now = self._now()
                self.db.execute(
                    "UPDATE offline_asr_jobs SET state='done',lease_owner=NULL,lease_until=NULL,"
                    "last_error=NULL,updated_at=? WHERE id=? AND state='running'", (now, job.job_id))
                self._wake.set()

    async def _claim(self, owner: str) -> OfflineJob | None:
        async with self._claim_lock:
            now = self._now()
            self.db.execute(
                "UPDATE offline_asr_jobs SET state='queued',lease_owner=NULL,lease_until=NULL,"
                "available_at=?,updated_at=? WHERE state='running' AND lease_until<?",
                (now, now, now))
            row = self.db.query_one(
                "SELECT j.* FROM offline_asr_jobs j WHERE j.state='queued' AND j.available_at<=?"
                " AND ("
                "   j.reason NOT IN ('canonical','summarize','publish')"
                "   OR (j.reason='canonical' AND NOT EXISTS ("
                "     SELECT 1 FROM offline_asr_jobs p WHERE p.session_id=j.session_id"
                "     AND p.reason NOT IN ('canonical','summarize','publish')"
                "     AND p.state IN ('queued','running')"
                "   ))"
                "   OR (j.reason IN ('summarize','publish') AND NOT EXISTS ("
                "     SELECT 1 FROM offline_asr_jobs p WHERE p.session_id=j.session_id"
                "     AND p.reason NOT IN ('summarize','publish') AND p.state IN ('queued','running')"
                "   ))"
                " )"
                " ORDER BY j.order_key,j.session_id,j.chunk_index,j.id LIMIT 1", (now,))
            if row is None:
                return None
            lease_until = (datetime.now(timezone.utc) + timedelta(
                seconds=self.lease_seconds)).isoformat()
            cursor = self.db.execute(
                "UPDATE offline_asr_jobs SET state='running',lease_owner=?,lease_until=?,updated_at=?"
                " WHERE id=? AND state='queued'", (owner, lease_until, now, int(row["id"])))
            if cursor.rowcount != 1:
                return None
            job = OfflineJob(str(row["session_id"]), int(row["start_ms"]),
                             int(row["end_ms"]), str(row["reason"]),
                             int(row["attempts"]), int(row["id"]))
            return job

    async def _persistent_failure(self, job: OfflineJob, exc: Exception) -> None:
        current = self.db.query_one("SELECT state FROM offline_asr_jobs WHERE id=?", (job.job_id,))
        if current is None or current["state"] == "cancelled":
            logger.info("offline_job_cancelled session_id=%s reason=%s",
                        job.session_id, job.reason)
            return
        attempts = int(job.attempts) + 1
        now_dt = datetime.now(timezone.utc)
        now = now_dt.isoformat()
        if attempts > self.max_retries:
            self.db.execute(
                "UPDATE offline_asr_jobs SET state='failed',attempts=?,lease_owner=NULL,"
                "lease_until=NULL,last_error=?,updated_at=? WHERE id=?",
                (attempts, f"{type(exc).__name__}: {exc}"[:1000], now, job.job_id))
            # 失败会话不得越过完成屏障生成一个看似成功但不完整的纪要。
            self.db.execute(
                "UPDATE offline_asr_jobs SET state='cancelled',updated_at=? WHERE session_id=?"
                " AND reason IN ('summarize','publish') AND state='queued'", (now, job.session_id))
            job.attempts = attempts
            logger.exception("offline_job_gave_up session_id=%s reason=%s",
                             job.session_id, job.reason)
            if self.on_give_up is not None:
                try:
                    await self.on_give_up(job, exc)
                except Exception:
                    logger.exception("offline_job_give_up_callback_failed session_id=%s",
                                     job.session_id)
            return
        delay = min(120, self.retry_base_seconds * (2 ** max(0, attempts - 1)))
        available = (now_dt + timedelta(seconds=delay)).isoformat()
        self.db.execute(
            "UPDATE offline_asr_jobs SET state='queued',attempts=?,available_at=?,"
            "lease_owner=NULL,lease_until=NULL,last_error=?,updated_at=? WHERE id=?",
            (attempts, available, f"{type(exc).__name__}: {exc}"[:1000], now, job.job_id))
        logger.warning("offline_job_retry session_id=%s reason=%s attempt=%d/%d error=%s",
                       job.session_id, job.reason, attempts, self.max_retries, exc)
        self._wake.set()

    async def _process(self, job: OfflineJob) -> None:
        if job.reason in {"summarize", "publish"}:
            # 完成屏障：补洞和 canonical 都已处理，只发布完整转写；旧 summarize
            # 任务升级后也走此路径，绝不自动调用模型。
            if self.on_summarize:
                await self.on_summarize(job.session_id)
            return
        if job.reason == "canonical":
            if self.on_canonical is None:
                raise RuntimeError("canonical finalization callback is not configured")
            expected_sha256 = None
            if self.db is not None:
                row = self.db.query_one(
                    "SELECT canonical_sha256 FROM device_sessions WHERE server_session_id=?",
                    (job.session_id,))
                expected_sha256 = str(row["canonical_sha256"] or "") if row else None
            payload = await self.offline.finalize(job.session_id, expected_sha256)
            await self.on_canonical(job.session_id, payload)
            logger.info(
                "offline_canonical_job_done session_id=%s segments=%d speakers=%d",
                job.session_id, len(payload.get("segments") or []),
                int(payload.get("speaker_count") or 0))
            return
        tl = self.get_timeline(job.session_id)
        if tl is None:
            logger.warning("offline_job_no_timeline session_id=%s", job.session_id)
            if self.db is not None and self.db.query_one(
                    "SELECT 1 FROM device_sessions WHERE server_session_id=?"
                    " AND status NOT IN ('done','cancelled')", (job.session_id,)) is not None:
                # 设备会话仍声明需要处理时，缺失会议元数据是数据不完整，不能记成成功。
                raise RuntimeError("meeting timeline metadata is missing")
            return
        pcm = self._read_pcm_range(job.session_id, job.start_ms, job.end_ms)
        if not pcm:
            logger.warning("offline_job_no_audio session_id=%s range=[%d,%d)", job.session_id, job.start_ms, job.end_ms)
            if job.reason in {"bulk", "finalize"} and job.end_ms > job.start_ms:
                # A completed non-empty recording without its authoritative
                # PCM is data loss, not a successful no-op.  Retry it and then
                # surface terminal `failed` through on_give_up.
                raise RuntimeError("authoritative PCM is missing or empty")
            return
        segments = await self.offline.transcribe(pcm, base_offset_ms=job.start_ms)
        if job.reason in ("finalize", "bulk"):
            # 整段：只填实时没覆盖的空洞，保留实时段（含说话人）
            patch = tl.fill_gaps(segments)
        else:
            # 指定缺口区间：原位替换
            patch = tl.apply_offline(job.start_ms, job.end_ms, segments)
        # 转写已成功应用（含 noop：内容已在），持久化的洞记录可删
        if job.reason in ("gap", "bulk") and self.on_gap_done:
            self.on_gap_done(job.session_id, job.start_ms, job.end_ms)
        if not patch.added and not patch.removed:
            logger.info("offline_job_noop session_id=%s reason=%s", job.session_id, job.reason)
            return
        await self.on_applied(job.session_id, tl, patch, job.reason)
        logger.info("offline_job_done session_id=%s reason=%s added=%d removed=%d",
                    job.session_id, job.reason, len(patch.added), len(patch.removed))

    def _read_pcm_range(self, session_id: str, start_ms: int, end_ms: int) -> bytes:
        # 通道B的完整音频在 .b.pcm；按 BYTES_PER_MS 定位区间读取
        path = self.audio_root / f"{session_id}.b.pcm"
        if not path.exists():
            # 可靠范围上传尚未 /complete 时使用已校验的稀疏临时文件；调度方只会投递
            # 被覆盖区间，因此不会读取空洞。
            path = self.audio_root / f"{session_id}.b.pcm.part"
        if not path.exists():
            return b""
        with path.open("rb") as f:
            f.seek(int(start_ms * BYTES_PER_MS))
            return f.read(int((end_ms - start_ms) * BYTES_PER_MS)) if end_ms > start_ms else f.read()
