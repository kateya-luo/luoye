"""Owner-scoped people, aliases and voiceprint memory for completed meetings."""
from __future__ import annotations

import json
import math
import os
import re
import uuid
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .db import loads_or


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_name(value: str) -> str:
    return re.sub(r"[\s·•._-]+", "", str(value or "")).casefold()


class MeetingMemory:
    def __init__(self, storage: Any):
        self.storage = storage
        self.db = storage.db
        self.embedding_url = os.getenv("SPEAKER_EMBEDDING_URL", "http://speaker:10100/embed")
        self.high_threshold = float(os.getenv("PERSON_MATCH_HIGH_THRESHOLD", "0.82"))
        self.suggest_threshold = float(os.getenv("PERSON_MATCH_SUGGEST_THRESHOLD", "0.72"))

    def list_people(self, owner: str) -> list[dict[str, Any]]:
        people = []
        for row in self.db.query(
                "SELECT * FROM people WHERE owner_user_id=? AND active=1 ORDER BY display_name", (owner,)):
            aliases = [item["alias"] for item in self.db.query(
                "SELECT alias FROM person_aliases WHERE person_id=? ORDER BY created_at", (row["id"],))]
            count = self.db.query_one(
                "SELECT COUNT(*) n FROM person_voiceprints WHERE person_id=? AND active=1", (row["id"],))
            people.append({"id": row["id"], "display_name": row["display_name"],
                           "role": row["role"] or "", "notes": row["notes"] or "",
                           "aliases": aliases, "voiceprint_count": int(count["n"] if count else 0)})
        return people

    def create_person(self, owner: str, display_name: str, role: str = "",
                      aliases: list[str] | None = None) -> dict[str, Any]:
        name = str(display_name or "").strip()[:100]
        if not name:
            raise ValueError("人员姓名不能为空")
        person_id = "person-" + uuid.uuid4().hex
        now = _now()
        self.db.execute(
            "INSERT INTO people(id,owner_user_id,display_name,role,created_at,updated_at) VALUES(?,?,?,?,?,?)",
            (person_id, owner, name, str(role or "")[:100], now, now))
        self._upsert_alias(person_id, owner, name, "display_name")
        for alias in aliases or []:
            self._upsert_alias(person_id, owner, alias, "user")
        return next(item for item in self.list_people(owner) if item["id"] == person_id)

    def update_person(self, owner: str, person_id: str, *, display_name: str | None = None,
                      role: str | None = None, aliases: list[str] | None = None) -> dict[str, Any]:
        row = self.db.query_one("SELECT * FROM people WHERE id=? AND owner_user_id=? AND active=1",
                                (person_id, owner))
        if row is None:
            raise FileNotFoundError(person_id)
        name = str(display_name if display_name is not None else row["display_name"]).strip()[:100]
        new_role = str(role if role is not None else (row["role"] or ""))[:100]
        self.db.execute("UPDATE people SET display_name=?,role=?,updated_at=? WHERE id=?",
                        (name, new_role, _now(), person_id))
        self._upsert_alias(person_id, owner, name, "display_name")
        for alias in aliases or []:
            self._upsert_alias(person_id, owner, alias, "user")
        # Existing meeting labels follow an explicitly renamed person.
        self.db.execute(
            "UPDATE meeting_speaker_assignments SET display_name=?,role=?,updated_at=? WHERE person_id=?",
            (name, new_role, _now(), person_id))
        self.db.execute("UPDATE segments SET speaker_label=? WHERE person_id=?", (name, person_id))
        return next(item for item in self.list_people(owner) if item["id"] == person_id)

    def delete_person(self, owner: str, person_id: str) -> bool:
        cursor = self.db.execute(
            "UPDATE people SET active=0,updated_at=? WHERE id=? AND owner_user_id=? AND active=1",
            (_now(), person_id, owner))
        self.db.execute("UPDATE person_voiceprints SET active=0,updated_at=? WHERE person_id=?",
                        (_now(), person_id))
        return cursor.rowcount == 1

    def _upsert_alias(self, person_id: str, owner: str, alias: str, source: str) -> None:
        value = str(alias or "").strip()[:100]
        normalized = _normalize_name(value)
        if not normalized:
            return
        existing = self.db.query_one(
            "SELECT person_id FROM person_aliases WHERE owner_user_id=? AND normalized_alias=?",
            (owner, normalized))
        if existing and existing["person_id"] != person_id:
            return
        self.db.execute(
            "INSERT INTO person_aliases(id,person_id,owner_user_id,alias,normalized_alias,source,created_at)"
            " VALUES(?,?,?,?,?,?,?) ON CONFLICT(owner_user_id,normalized_alias) DO UPDATE SET"
            " person_id=excluded.person_id,alias=excluded.alias,source=excluded.source",
            ("alias-" + uuid.uuid4().hex, person_id, owner, value, normalized, source, _now()))
        self.db.execute(
            "INSERT INTO lexicon_entries(id,owner_user_id,canonical_text,variants_json,kind,person_id,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(owner_user_id,canonical_text,kind) DO UPDATE SET"
            " person_id=excluded.person_id,active=1,updated_at=excluded.updated_at",
            ("lex-" + uuid.uuid4().hex, owner,
             self.db.query_one("SELECT display_name FROM people WHERE id=?", (person_id,))["display_name"],
             json.dumps([value], ensure_ascii=False), "person", person_id, _now(), _now()))

    def session_speakers(self, session_id: str, owner: str) -> list[dict[str, Any]]:
        if not self.storage.user_owns_meeting(session_id, owner):
            raise FileNotFoundError(session_id)
        rows = self.db.query(
            "SELECT speaker_id,COALESCE(MAX(speaker_label),'') speaker_label,COUNT(*) segment_count,"
            "SUM(end_ms-start_ms) duration_ms FROM segments WHERE session_id=? AND speaker_id IS NOT NULL"
            " GROUP BY speaker_id ORDER BY MIN(start_ms)", (session_id,))
        assignments = {r["speaker_id"]: dict(r) for r in self.db.query(
            "SELECT * FROM meeting_speaker_assignments WHERE session_id=?", (session_id,))}
        result = []
        for row in rows:
            assignment = assignments.get(row["speaker_id"], {})
            result.append({
                "speaker_id": row["speaker_id"],
                "display_name": assignment.get("display_name") or row["speaker_label"] or row["speaker_id"],
                "role": assignment.get("role") or "", "person_id": assignment.get("person_id"),
                "match_confidence": assignment.get("match_confidence"),
                "match_mode": assignment.get("match_mode") or "meeting_local",
                "remembered": bool(assignment.get("remembered")),
                "segment_count": int(row["segment_count"]), "duration_ms": int(row["duration_ms"] or 0),
            })
        return result

    async def assign_speaker(self, session_id: str, owner: str, speaker_id: str, *,
                             display_name: str, role: str = "", person_id: str | None = None,
                             remember: bool = False) -> dict[str, Any]:
        if not self.storage.user_owns_meeting(session_id, owner):
            raise FileNotFoundError(session_id)
        current = self.db.query_one(
            "SELECT speaker_label FROM segments WHERE session_id=? AND speaker_id=? LIMIT 1",
            (session_id, speaker_id))
        if current is None:
            raise ValueError("会议中不存在该说话人")
        name = str(display_name or "").strip()[:100]
        if not name:
            raise ValueError("显示姓名不能为空")
        if remember and not person_id:
            person = self.create_person(owner, name, role, [current["speaker_label"] or ""])
            person_id = person["id"]
        if person_id:
            person = self.db.query_one("SELECT * FROM people WHERE id=? AND owner_user_id=? AND active=1",
                                       (person_id, owner))
            if person is None:
                raise ValueError("人员记忆不存在")
            name = person["display_name"]
            role = str(role or person["role"] or "")
            self._upsert_alias(person_id, owner, current["speaker_label"] or "", "speaker_correction")
        now = _now()
        with self.db.transaction() as conn:
            conn.execute(
                "INSERT INTO meeting_speaker_assignments(session_id,speaker_id,owner_user_id,person_id,"
                "display_name,role,match_confidence,match_mode,remembered,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id,speaker_id) DO UPDATE SET person_id=excluded.person_id,"
                "display_name=excluded.display_name,role=excluded.role,match_confidence=excluded.match_confidence,"
                "match_mode=excluded.match_mode,remembered=excluded.remembered,updated_at=excluded.updated_at",
                (session_id, speaker_id, owner, person_id, name, role, 1.0, "manual", int(remember), now))
            conn.execute("UPDATE segments SET speaker_label=?,person_id=? WHERE session_id=? AND speaker_id=?",
                         (name, person_id, session_id, speaker_id))
            conn.execute(
                "UPDATE meetings SET speaker_revision=speaker_revision+1,minutes_status="
                "CASE WHEN minutes_status='ready' THEN 'outdated' ELSE minutes_status END,updated_at=?"
                " WHERE session_id=?", (now, session_id))
        if remember and person_id:
            await self.enroll_voiceprint(session_id, owner, speaker_id, person_id)
        return next(item for item in self.session_speakers(session_id, owner)
                    if item["speaker_id"] == speaker_id)

    async def enroll_voiceprint(self, session_id: str, owner: str, speaker_id: str,
                                person_id: str) -> dict[str, Any] | None:
        existing = self.db.query_one(
            "SELECT id,sample_count FROM person_voiceprints WHERE person_id=? AND owner_user_id=?"
            " AND source_session_id=? AND source_speaker_id=? AND active=1",
            (person_id, owner, session_id, speaker_id))
        if existing:
            return {"id": existing["id"], "sample_count": int(existing["sample_count"] or 0)}
        segments = [s for s in self.storage.load_segments(session_id)
                    if s.get("speaker_id") == speaker_id and int(s.get("end_ms") or 0) - int(s.get("start_ms") or 0) >= 1200]
        segments.sort(key=lambda s: int(s.get("end_ms") or 0) - int(s.get("start_ms") or 0), reverse=True)
        embeddings = await self._speaker_embeddings(session_id, segments[:5])
        if not embeddings:
            return None
        centroid = self._centroid(embeddings)
        now = _now()
        voice_id = "voice-" + uuid.uuid4().hex
        self.db.execute(
            "INSERT INTO person_voiceprints(id,person_id,owner_user_id,embedding_json,sample_count,quality,"
            "source_session_id,source_speaker_id,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (voice_id, person_id, owner, json.dumps(centroid), len(embeddings),
             min(1.0, len(embeddings) / 3), session_id, speaker_id, now, now))
        return {"id": voice_id, "sample_count": len(embeddings)}

    async def match_session(self, session_id: str) -> list[dict[str, Any]]:
        owner = self.storage.meeting_owner(session_id)
        if not owner:
            return []
        voiceprints = []
        for row in self.db.query(
                "SELECT v.*,p.display_name,p.role FROM person_voiceprints v JOIN people p ON p.id=v.person_id"
                " WHERE v.owner_user_id=? AND v.active=1 AND p.active=1", (owner,)):
            voiceprints.append((dict(row), loads_or(row["embedding_json"], [])))
        if not voiceprints:
            return []
        speakers = self.session_speakers(session_id, owner)
        now = _now()
        matches = []
        for speaker in speakers:
            segments = [s for s in self.storage.load_segments(session_id)
                        if s.get("speaker_id") == speaker["speaker_id"]
                        and int(s.get("end_ms") or 0) - int(s.get("start_ms") or 0) >= 1200]
            segments.sort(key=lambda s: int(s.get("end_ms") or 0) - int(s.get("start_ms") or 0), reverse=True)
            embeddings = await self._speaker_embeddings(session_id, segments[:4])
            if not embeddings:
                continue
            centroid = self._centroid(embeddings)
            scored = [(self._cosine(centroid, vector), row) for row, vector in voiceprints if vector]
            if not scored:
                continue
            score, person = max(scored, key=lambda item: item[0])
            mode = "auto" if score >= self.high_threshold else "suggested" if score >= self.suggest_threshold else "unmatched"
            if mode == "unmatched":
                continue
            self.db.execute(
                "INSERT INTO meeting_speaker_assignments(session_id,speaker_id,owner_user_id,person_id,"
                "display_name,role,match_confidence,match_mode,remembered,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)"
                " ON CONFLICT(session_id,speaker_id) DO UPDATE SET person_id=excluded.person_id,"
                "display_name=excluded.display_name,role=excluded.role,match_confidence=excluded.match_confidence,"
                "match_mode=excluded.match_mode,updated_at=excluded.updated_at",
                (session_id, speaker["speaker_id"], owner, person["person_id"], person["display_name"],
                 person["role"] or "", score, mode, 1, now))
            if mode == "auto":
                self.db.execute("UPDATE segments SET speaker_label=?,person_id=? WHERE session_id=? AND speaker_id=?",
                                (person["display_name"], person["person_id"], session_id, speaker["speaker_id"]))
            matches.append({"speaker_id": speaker["speaker_id"], "person_id": person["person_id"],
                            "display_name": person["display_name"], "confidence": round(score, 4), "mode": mode})
        return matches

    async def _speaker_embeddings(self, session_id: str, segments: list[dict[str, Any]]) -> list[list[float]]:
        path = self.storage.root / "audio_cache" / f"{session_id}.b.pcm"
        if not path.exists():
            path = self.storage.root / "audio_cache" / f"{session_id}.pcm"
        if not path.exists():
            return []
        vectors = []
        with path.open("rb") as audio:
            async with httpx.AsyncClient(timeout=20) as client:
                for segment in segments:
                    start = max(0, int(segment.get("start_ms") or 0)) * 32
                    length = min(10_000, int(segment.get("end_ms") or 0) - int(segment.get("start_ms") or 0)) * 32
                    audio.seek(start)
                    pcm = audio.read(length)
                    if len(pcm) < 32_000:
                        continue
                    try:
                        response = await client.post(self.embedding_url, content=pcm, headers={
                            "Content-Type": "application/octet-stream", "X-Audio-Format": "pcm_s16le",
                            "X-Sample-Rate": "16000"})
                        response.raise_for_status()
                        vector = response.json().get("embedding")
                        if isinstance(vector, list) and vector:
                            vectors.append(self._normalize([float(v) for v in vector]))
                    except (httpx.HTTPError, ValueError):
                        continue
        return vectors

    @classmethod
    def _centroid(cls, vectors: list[list[float]]) -> list[float]:
        return cls._normalize([sum(values) / len(vectors) for values in zip(*vectors)])

    @staticmethod
    def _normalize(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(v * v for v in vector))
        return [v / norm for v in vector] if norm else []

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right))

    def confirmed_context(self, owner: str) -> dict[str, Any]:
        people = [{"name": p["display_name"], "role": p["role"], "aliases": p["aliases"]}
                  for p in self.list_people(owner)]
        facts = [dict(row) for row in self.db.query(
            "SELECT kind,content,source_session_id FROM memory_facts WHERE owner_user_id=?"
            " AND status='confirmed' ORDER BY updated_at DESC LIMIT 100", (owner,))]
        terms = [dict(row) for row in self.db.query(
            "SELECT canonical_text,variants_json,kind FROM lexicon_entries WHERE owner_user_id=? AND active=1",
            (owner,))]
        return {"people": people, "confirmed_facts": facts, "lexicon": terms}

    def confirm_candidates(self, owner: str, session_id: str,
                           candidates: list[dict[str, Any]]) -> list[str]:
        if not self.storage.user_owns_meeting(session_id, owner):
            raise FileNotFoundError(session_id)
        ids = []
        for item in candidates:
            content = str(item.get("content") or "").strip()[:1000]
            if not content:
                continue
            kind = str(item.get("kind") or "project_fact")[:40]
            existing = self.db.query_one(
                "SELECT id FROM memory_facts WHERE owner_user_id=? AND kind=? AND content=?"
                " AND status='confirmed' LIMIT 1", (owner, kind, content))
            if existing:
                ids.append(existing["id"])
                continue
            fact_id = "fact-" + uuid.uuid4().hex
            now = _now()
            self.db.execute(
                "INSERT INTO memory_facts(id,owner_user_id,source_session_id,kind,content,status,created_at,updated_at)"
                " VALUES(?,?,?,?,?,'confirmed',?,?)",
                (fact_id, owner, session_id, kind, content, now, now))
            ids.append(fact_id)
        return ids
