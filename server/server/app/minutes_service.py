"""Explicit, template-driven post-meeting minutes and memory APIs."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from .auth import CurrentUser, require_auth
from .deepseek_client import DeepSeekClient
from .meeting_memory import MeetingMemory
from .minutes_templates import get_template, templates

logger = logging.getLogger("ai_recorder.minutes")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class MinutesCreate(BaseModel):
    template_id: str = Field(min_length=1, max_length=8)
    template_version: int = 1
    output_language: str = "zh"


class SpeakerUpdate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    role: str = Field(default="", max_length=100)
    person_id: str | None = None
    remember: bool = False


class PersonCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    role: str = Field(default="", max_length=100)
    aliases: list[str] = []


class PersonUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    role: str | None = Field(default=None, max_length=100)
    aliases: list[str] | None = None


class CandidateConfirm(BaseModel):
    candidates: list[dict[str, Any]]


class SegmentTextUpdate(BaseModel):
    text: str = Field(min_length=1, max_length=8000)


class MergeSpeakers(BaseModel):
    source_speaker_id: str
    target_speaker_id: str


class MinutesService:
    def __init__(self, storage: Any, memory: MeetingMemory):
        self.storage = storage
        self.db = storage.db
        self.memory = memory
        self.llm = DeepSeekClient()
        self.tasks: dict[str, asyncio.Task] = {}
        self._seed_templates()

    def _seed_templates(self) -> None:
        now = _now()
        for template in templates():
            self.db.execute(
                "INSERT INTO minutes_templates(template_id,version,name,category,description,schema_json,"
                "prompt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(template_id,version) DO UPDATE SET name=excluded.name,category=excluded.category,"
                "description=excluded.description,schema_json=excluded.schema_json,prompt_json=excluded.prompt_json,"
                "active=1,updated_at=excluded.updated_at",
                (template["id"], template["version"], template["name"], template["category"],
                 template["description"], json.dumps({"sections": template["sections"]}, ensure_ascii=False),
                 json.dumps(template, ensure_ascii=False), now, now))

    def start(self) -> None:
        # A process interruption must never create a second paid request silently.
        # Queued jobs are safe to resume; generating jobs are failed and require an explicit retry.
        now = _now()
        self.db.execute(
            "UPDATE minutes_jobs SET state='failed',last_error='服务器在模型请求期间重启；为避免重复扣费，请手动重试',"
            "updated_at=? WHERE state='generating'", (now,))
        for row in self.db.query("SELECT id FROM minutes_jobs WHERE state='queued' ORDER BY created_at"):
            self._schedule(row["id"])

    async def stop(self) -> None:
        active = [task for task in self.tasks.values() if not task.done()]
        for task in active:
            task.cancel()
        if active:
            await asyncio.gather(*active, return_exceptions=True)

    def list_templates(self) -> list[dict[str, Any]]:
        return templates()

    def create_job(self, session_id: str, owner: str, body: MinutesCreate) -> dict[str, Any]:
        meeting = self.storage.get_meeting(session_id, owner)
        if meeting is None:
            raise FileNotFoundError(session_id)
        state = self.storage.get_state(session_id)
        if state not in {"transcript_ready", "done"}:
            raise ValueError("完整转写尚未准备好")
        template = get_template(body.template_id, body.template_version)
        if template is None:
            raise ValueError("纪要模板不存在")
        snapshot = self._snapshot(session_id, owner)
        max_chars = int(os.getenv("MAX_MINUTES_TRANSCRIPT_CHARS", "600000"))
        if len(snapshot["transcript"]) > max_chars:
            raise ValueError(f"转写超过单次模型调用安全上限（{max_chars} 字符），尚未发起付费请求")
        model = os.getenv("DEEPSEEK_MINUTES_MODEL", "deepseek-v4-flash")
        identity = json.dumps({
            "session_id": session_id, "transcript_sha256": snapshot["sha256"],
            "transcript_revision": snapshot["transcript_revision"],
            "speaker_revision": snapshot["speaker_revision"], "template_id": template["id"],
            "template_version": template["version"], "language": body.output_language, "model": model,
        }, sort_keys=True, ensure_ascii=False)
        request_hash = hashlib.sha256(identity.encode()).hexdigest()
        existing = self.db.query_one("SELECT * FROM minutes_jobs WHERE request_hash=?", (request_hash,))
        if existing is not None:
            return self._job(existing["id"])
        job_id = "minutes-" + uuid.uuid4().hex
        now = _now()
        self.db.execute(
            "INSERT INTO minutes_jobs(id,session_id,owner_user_id,template_id,template_version,request_hash,"
            "transcript_revision,speaker_revision,transcript_sha256,model,state,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,'queued',?,?)",
            (job_id, session_id, owner, template["id"], template["version"], request_hash,
             snapshot["transcript_revision"], snapshot["speaker_revision"], snapshot["sha256"], model, now, now))
        self.db.execute("UPDATE meetings SET minutes_status='queued',updated_at=? WHERE session_id=?",
                        (now, session_id))
        self._schedule(job_id, output_language=body.output_language)
        logger.info("minutes_job_queued session_id=%s job_id=%s template=%s transcript_chars=%d",
                    session_id, job_id, template["id"], len(snapshot["transcript"]))
        return self._job(job_id)

    def _schedule(self, job_id: str, output_language: str = "zh") -> None:
        if job_id in self.tasks and not self.tasks[job_id].done():
            return
        task = asyncio.create_task(self._run(job_id, output_language), name=job_id)
        self.tasks[job_id] = task
        task.add_done_callback(lambda _task: self.tasks.pop(job_id, None))

    async def _run(self, job_id: str, output_language: str) -> None:
        row = self.db.query_one("SELECT * FROM minutes_jobs WHERE id=?", (job_id,))
        if row is None or row["state"] != "queued":
            return
        self.db.execute("UPDATE minutes_jobs SET state='generating',attempts=attempts+1,updated_at=? WHERE id=?",
                        (_now(), job_id))
        self.db.execute("UPDATE meetings SET minutes_status='generating',updated_at=? WHERE session_id=?",
                        (_now(), row["session_id"]))
        try:
            template = get_template(row["template_id"], int(row["template_version"]))
            if template is None:
                raise RuntimeError("模板已被移除")
            snapshot = self._snapshot(row["session_id"], row["owner_user_id"])
            if snapshot["sha256"] != row["transcript_sha256"]:
                raise RuntimeError("转写或人员映射已发生变化，请重新点击生成")
            meeting = self.storage.get_meeting(row["session_id"], row["owner_user_id"]) or {}
            result, metrics = await self.llm.generate_template_minutes(
                snapshot["transcript"], template,
                meeting_context={"session_id": row["session_id"], "title": meeting.get("title"),
                                 "created_at": meeting.get("created_at"),
                                 "speakers": self.memory.session_speakers(row["session_id"], row["owner_user_id"])},
                memory_context=self.memory.confirmed_context(row["owner_user_id"]),
                output_language=output_language)
            result["generation"] = {**metrics, "job_id": job_id,
                                    "transcript_sha256": row["transcript_sha256"]}
            version_id = "minutes-version-" + uuid.uuid4().hex
            now = _now()
            with self.db.transaction() as conn:
                conn.execute("UPDATE minutes_versions SET active=0 WHERE session_id=?", (row["session_id"],))
                conn.execute(
                    "INSERT INTO minutes_versions(id,job_id,session_id,owner_user_id,template_id,template_version,"
                    "transcript_revision,speaker_revision,transcript_sha256,result_json,active,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,1,?)",
                    (version_id, job_id, row["session_id"], row["owner_user_id"], row["template_id"],
                     row["template_version"], row["transcript_revision"], row["speaker_revision"],
                     row["transcript_sha256"], json.dumps(result, ensure_ascii=False), now))
                conn.execute(
                    "UPDATE minutes_jobs SET state='ready',result_json=?,model=?,prompt_tokens=?,completion_tokens=?,"
                    "cache_hit_tokens=?,elapsed_ms=?,last_error=NULL,updated_at=? WHERE id=?",
                    (json.dumps(result, ensure_ascii=False), metrics["model"], metrics["prompt_tokens"],
                     metrics["completion_tokens"], metrics["cache_hit_tokens"], metrics["elapsed_ms"], now, job_id))
                conn.execute(
                    "UPDATE meetings SET summary_json=?,minutes_status='ready',active_minutes_version_id=?,updated_at=?"
                    " WHERE session_id=?",
                    (json.dumps(result, ensure_ascii=False), version_id, now, row["session_id"]))
            logger.info("minutes_job_ready session_id=%s job_id=%s template=%s elapsed_ms=%d prompt_tokens=%d completion_tokens=%d",
                        row["session_id"], job_id, row["template_id"], metrics["elapsed_ms"],
                        metrics["prompt_tokens"], metrics["completion_tokens"])
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            now = _now()
            self.db.execute("UPDATE minutes_jobs SET state='failed',last_error=?,updated_at=? WHERE id=?",
                            (f"{type(exc).__name__}: {exc}"[:1000], now, job_id))
            self.db.execute("UPDATE meetings SET minutes_status='failed',updated_at=? WHERE session_id=?",
                            (now, row["session_id"]))
            logger.exception("minutes_job_failed session_id=%s job_id=%s", row["session_id"], job_id)

    def _snapshot(self, session_id: str, owner: str) -> dict[str, Any]:
        meeting = self.db.query_one(
            "SELECT transcript_revision,speaker_revision FROM meetings WHERE session_id=? AND owner_user_id=?",
            (session_id, owner))
        if meeting is None:
            raise FileNotFoundError(session_id)
        lines = []
        for seg in self.storage.load_segments(session_id):
            text = str(seg.get("text") or "").strip()
            if not text:
                continue
            start = int(seg.get("start_ms") or 0)
            label = str(seg.get("speaker_label") or seg.get("speaker_id") or "未知说话人")
            lines.append(f"[t={start}ms][{label}] {text}")
        transcript = "\n".join(lines)
        return {"transcript": transcript, "sha256": hashlib.sha256(transcript.encode()).hexdigest(),
                "transcript_revision": int(meeting["transcript_revision"] or 0),
                "speaker_revision": int(meeting["speaker_revision"] or 0)}

    def _job(self, job_id: str, owner: str | None = None) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM minutes_jobs WHERE id=?", (job_id,))
        if row is None or (owner is not None and row["owner_user_id"] != owner):
            raise FileNotFoundError(job_id)
        return {"id": row["id"], "session_id": row["session_id"], "template_id": row["template_id"],
                "template_version": row["template_version"], "state": row["state"],
                "attempts": row["attempts"], "model": row["model"],
                "prompt_tokens": row["prompt_tokens"], "completion_tokens": row["completion_tokens"],
                "cache_hit_tokens": row["cache_hit_tokens"], "elapsed_ms": row["elapsed_ms"],
                "last_error": row["last_error"],
                "result": json.loads(row["result_json"]) if row["result_json"] else None,
                "created_at": row["created_at"], "updated_at": row["updated_at"]}

    def list_versions(self, session_id: str, owner: str) -> list[dict[str, Any]]:
        if not self.storage.user_owns_meeting(session_id, owner):
            raise FileNotFoundError(session_id)
        return [{"id": row["id"], "template_id": row["template_id"],
                 "template_version": row["template_version"], "active": bool(row["active"]),
                 "outdated": int(row["transcript_revision"]) != current[0]
                             or int(row["speaker_revision"]) != current[1],
                 "created_at": row["created_at"], "result": json.loads(row["result_json"])}
                for row in self.db.query(
                    "SELECT * FROM minutes_versions WHERE session_id=? ORDER BY created_at DESC", (session_id,))
                for current in [(int(self.db.query_one("SELECT transcript_revision FROM meetings WHERE session_id=?", (session_id,))["transcript_revision"] or 0),
                                 int(self.db.query_one("SELECT speaker_revision FROM meetings WHERE session_id=?", (session_id,))["speaker_revision"] or 0))]]


def create_minutes_router(storage: Any, service: MinutesService, memory: MeetingMemory,
                          *, prefix: str = "/api/v1") -> APIRouter:
    router = APIRouter(prefix=prefix, tags=["minutes-memory"])

    def not_found(message: str = "资源不存在") -> HTTPException:
        return HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=message)

    @router.get("/minutes/templates")
    async def list_templates(_user: CurrentUser = Depends(require_auth)):
        return {"templates": service.list_templates()}

    @router.post("/meetings/{session_id}/minutes/jobs", status_code=202)
    async def create_minutes(session_id: str, body: MinutesCreate,
                             user: CurrentUser = Depends(require_auth)):
        try:
            return service.create_job(session_id, user.id, body)
        except FileNotFoundError:
            raise not_found("会议不存在")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.get("/minutes/jobs/{job_id}")
    async def get_job(job_id: str, user: CurrentUser = Depends(require_auth)):
        try:
            return service._job(job_id, user.id)
        except FileNotFoundError:
            raise not_found("纪要任务不存在")

    @router.get("/meetings/{session_id}/minutes/versions")
    async def versions(session_id: str, user: CurrentUser = Depends(require_auth)):
        try:
            return {"versions": service.list_versions(session_id, user.id)}
        except FileNotFoundError:
            raise not_found("会议不存在")

    @router.get("/meetings/{session_id}/speakers")
    async def speakers(session_id: str, user: CurrentUser = Depends(require_auth)):
        try:
            return {"speakers": memory.session_speakers(session_id, user.id)}
        except FileNotFoundError:
            raise not_found("会议不存在")

    @router.patch("/meetings/{session_id}/speakers/{speaker_id}")
    async def update_speaker(session_id: str, speaker_id: str, body: SpeakerUpdate,
                             user: CurrentUser = Depends(require_auth)):
        try:
            return await memory.assign_speaker(session_id, user.id, speaker_id,
                                               display_name=body.display_name, role=body.role,
                                               person_id=body.person_id, remember=body.remember)
        except FileNotFoundError:
            raise not_found("会议不存在")
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.post("/meetings/{session_id}/speakers/merge")
    async def merge_speakers(session_id: str, body: MergeSpeakers,
                             user: CurrentUser = Depends(require_auth)):
        if not storage.user_owns_meeting(session_id, user.id):
            raise not_found("会议不存在")
        if body.source_speaker_id == body.target_speaker_id:
            raise HTTPException(status_code=400, detail="不能合并同一个说话人")
        target = storage.db.query_one(
            "SELECT speaker_label,person_id FROM segments WHERE session_id=? AND speaker_id=? LIMIT 1",
            (session_id, body.target_speaker_id))
        if target is None:
            raise HTTPException(status_code=409, detail="目标说话人不存在")
        cursor = storage.db.execute(
            "UPDATE segments SET speaker_id=?,speaker_label=?,person_id=? WHERE session_id=? AND speaker_id=?",
            (body.target_speaker_id, target["speaker_label"], target["person_id"],
             session_id, body.source_speaker_id))
        if cursor.rowcount == 0:
            raise HTTPException(status_code=409, detail="来源说话人不存在")
        storage.db.execute(
            "UPDATE meetings SET speaker_revision=speaker_revision+1,minutes_status="
            "CASE WHEN minutes_status='ready' THEN 'outdated' ELSE minutes_status END,updated_at=? WHERE session_id=?",
            (_now(), session_id))
        return {"merged": True, "updated_segments": cursor.rowcount}

    @router.patch("/meetings/{session_id}/segments/{seg_id}")
    async def update_segment(session_id: str, seg_id: str, body: SegmentTextUpdate,
                             user: CurrentUser = Depends(require_auth)):
        if not storage.user_owns_meeting(session_id, user.id):
            raise not_found("会议不存在")
        row = storage.db.query_one("SELECT text,raw_text FROM segments WHERE session_id=? AND seg_id=?",
                                   (session_id, seg_id))
        if row is None:
            raise not_found("字幕不存在")
        storage.db.execute("UPDATE segments SET raw_text=COALESCE(raw_text,text),text=?,revision=revision+1"
                           " WHERE session_id=? AND seg_id=?", (body.text.strip(), session_id, seg_id))
        storage.db.execute(
            "UPDATE meetings SET transcript_revision=transcript_revision+1,minutes_status="
            "CASE WHEN minutes_status='ready' THEN 'outdated' ELSE minutes_status END,updated_at=? WHERE session_id=?",
            (_now(), session_id))
        return {"ok": True, "raw_text": row["raw_text"] or row["text"], "text": body.text.strip()}

    @router.get("/memory/people")
    async def people(user: CurrentUser = Depends(require_auth)):
        return {"people": memory.list_people(user.id)}

    @router.post("/memory/people", status_code=201)
    async def create_person(body: PersonCreate, user: CurrentUser = Depends(require_auth)):
        try:
            return memory.create_person(user.id, body.display_name, body.role, body.aliases)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

    @router.patch("/memory/people/{person_id}")
    async def update_person(person_id: str, body: PersonUpdate,
                            user: CurrentUser = Depends(require_auth)):
        try:
            return memory.update_person(user.id, person_id, display_name=body.display_name,
                                        role=body.role, aliases=body.aliases)
        except FileNotFoundError:
            raise not_found("人员记忆不存在")

    @router.delete("/memory/people/{person_id}")
    async def delete_person(person_id: str, user: CurrentUser = Depends(require_auth)):
        if not memory.delete_person(user.id, person_id):
            raise not_found("人员记忆不存在")
        return {"deleted": True}

    @router.post("/meetings/{session_id}/memory/confirm")
    async def confirm_memory(session_id: str, body: CandidateConfirm,
                             user: CurrentUser = Depends(require_auth)):
        try:
            return {"confirmed_ids": memory.confirm_candidates(user.id, session_id, body.candidates)}
        except FileNotFoundError:
            raise not_found("会议不存在")

    return router
