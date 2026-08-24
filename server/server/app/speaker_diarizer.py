import asyncio
import logging
import math
import os
from array import array
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger("ai_recorder.speaker")


@dataclass(frozen=True)
class SpeakerDecision:
    """One live voiceprint decision; only confirmed decisions have an ID."""

    speaker_id: str | None
    state: str
    confidence: float = 0.0


class SpeakerDiarizer:
    """Assign stable, meeting-local speaker IDs from CAM++ embeddings.

    The meeting has no configured participant limit. The first voice becomes
    the anchor speaker; every later identity must be observed consistently
    before it is promoted from a candidate to a visible speaker.
    """

    sample_rate = 16000
    sample_width = 2

    def __init__(self):
        self.mode = os.getenv("SPEAKER_MODE", "off").strip().lower()
        self.url = os.getenv("SPEAKER_EMBEDDING_URL", "http://speaker:10100/embed")
        self.threshold = float(os.getenv("SPEAKER_SIMILARITY_THRESHOLD", "0.68"))
        self.candidate_threshold = float(os.getenv(
            "SPEAKER_CANDIDATE_SIMILARITY_THRESHOLD", "0.72"))
        self.merge_threshold = float(os.getenv("SPEAKER_CLUSTER_MERGE_THRESHOLD", "0.78"))
        self.min_seconds = max(0.5, float(os.getenv("SPEAKER_MIN_SEGMENT_SECONDS", "2.0")))
        self.max_seconds = max(
            self.min_seconds, float(os.getenv("SPEAKER_MAX_SEGMENT_SECONDS", "12")))
        self.min_bytes = int(self.min_seconds * self.sample_rate * self.sample_width)
        self.max_bytes = int(self.max_seconds * self.sample_rate * self.sample_width)
        self.min_rms = max(0, int(os.getenv("SPEAKER_MIN_RMS", "80")))
        self.candidate_confirmations = max(
            2, int(os.getenv("SPEAKER_CANDIDATE_CONFIRMATIONS", "2")))
        self.candidate_ttl = max(
            self.candidate_confirmations, int(os.getenv("SPEAKER_CANDIDATE_TTL_SEGMENTS", "12")))
        self.merge_interval = max(2, int(os.getenv("SPEAKER_CLUSTER_MERGE_INTERVAL", "20")))
        self.timeout = float(os.getenv("SPEAKER_REQUEST_TIMEOUT_SECONDS", "20"))
        self.centroids: list[list[float]] = []
        self.counts: list[int] = []
        self.candidates: list[dict[str, Any]] = []
        self.aliases: dict[int, int] = {}
        self.assigned_counts: dict[str, int] = {}
        self.segment_count = 0
        self.failed_count = 0
        self.skipped_short_count = 0
        self.skipped_quiet_count = 0
        self.candidate_count = 0
        self.merge_count = 0
        self.last_error = ""

    async def assign(self, pcm: bytes) -> str | None:
        return (await self.assign_detailed(pcm)).speaker_id

    async def assign_detailed(self, pcm: bytes) -> SpeakerDecision:
        if self.mode == "off":
            return SpeakerDecision(None, "disabled")
        prepared = self._select_quality_window(pcm)
        if prepared is None:
            return SpeakerDecision(None, "skipped")
        self.segment_count += 1
        if self.mode == "mock":
            speaker_id = f"spk_{((self.segment_count - 1) % 2) + 1:02d}"
            self.assigned_counts[speaker_id] = self.assigned_counts.get(speaker_id, 0) + 1
            return SpeakerDecision(speaker_id, "confirmed", 1.0)
        try:
            embedding = await self._fetch_embedding(prepared)
            decision = self._classify(embedding)
            if decision.speaker_id:
                speaker_id = self._canonical_speaker_id(decision.speaker_id)
                decision = SpeakerDecision(speaker_id, decision.state, decision.confidence)
                self.assigned_counts[speaker_id] = self.assigned_counts.get(speaker_id, 0) + 1
            if self.segment_count % self.merge_interval == 0:
                self._merge_confirmed_clusters()
            self._expire_candidates()
            self.last_error = ""
            return decision
        except Exception as exc:
            self.failed_count += 1
            self.last_error = str(exc)[:240]
            logger.warning("speaker_assignment_failed bytes=%d error=%s", len(prepared), exc)
            return SpeakerDecision(None, "failed")

    def _select_quality_window(self, pcm: bytes) -> bytes | None:
        pcm = pcm[:len(pcm) - (len(pcm) % self.sample_width)]
        if len(pcm) < self.min_bytes:
            self.skipped_short_count += 1
            return None
        if len(pcm) <= self.max_bytes:
            candidates = [pcm]
        else:
            last_start = len(pcm) - self.max_bytes
            middle = last_start // 2
            middle -= middle % self.sample_width
            starts = sorted({0, middle, last_start})
            candidates = [pcm[start:start + self.max_bytes] for start in starts]
        rms, selected = max(
            ((self._rms(window), window) for window in candidates), key=lambda item: item[0])
        if rms < self.min_rms:
            self.skipped_quiet_count += 1
            return None
        return selected

    @staticmethod
    def _rms(pcm: bytes) -> int:
        samples = array("h")
        samples.frombytes(pcm)
        if not samples:
            return 0
        return int(math.sqrt(sum(int(value) * int(value) for value in samples) / len(samples)))

    async def _fetch_embedding(self, pcm: bytes) -> list[float]:
        headers = {
            "Content-Type": "application/octet-stream",
            "X-Audio-Format": "pcm_s16le",
            "X-Sample-Rate": str(self.sample_rate),
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            for attempt in range(2):
                try:
                    response = await client.post(self.url, content=pcm, headers=headers)
                    response.raise_for_status()
                    payload: dict[str, Any] = response.json()
                    break
                except (httpx.HTTPError, ValueError):
                    if attempt:
                        raise
                    await asyncio.sleep(0.15)
        vector = payload.get("embedding")
        if not isinstance(vector, list) or not vector:
            raise ValueError("speaker service returned no embedding")
        return self._normalize([float(value) for value in vector])

    def _classify(self, embedding: list[float]) -> SpeakerDecision:
        embedding = self._normalize(embedding)
        if not self.centroids:
            self.centroids.append(embedding)
            self.counts.append(1)
            return SpeakerDecision("spk_01", "confirmed", 1.0)

        similarities = [self._cosine(embedding, center) for center in self.centroids]
        best = max(range(len(similarities)), key=similarities.__getitem__)
        similarity = similarities[best]
        if similarity >= self.threshold:
            self._update_cluster(best, embedding)
            return SpeakerDecision(f"spk_{best + 1:02d}", "confirmed", similarity)

        candidate_best = None
        candidate_similarity = -1.0
        for index, candidate in enumerate(self.candidates):
            value = self._cosine(embedding, candidate["centroid"])
            if value > candidate_similarity:
                candidate_best, candidate_similarity = index, value
        if candidate_best is None or candidate_similarity < self.candidate_threshold:
            self.candidates.append({
                "centroid": embedding, "count": 1, "last_seen": self.segment_count,
            })
            self.candidate_count += 1
            return SpeakerDecision(None, "candidate", max(0.0, similarity))

        candidate = self.candidates[candidate_best]
        count = int(candidate["count"])
        candidate["centroid"] = self._weighted_centroid(
            candidate["centroid"], count, embedding, 1)
        candidate["count"] = count + 1
        candidate["last_seen"] = self.segment_count
        if candidate["count"] < self.candidate_confirmations:
            return SpeakerDecision(None, "candidate", candidate_similarity)

        self.centroids.append(candidate["centroid"])
        self.counts.append(int(candidate["count"]))
        self.candidates.pop(candidate_best)
        return SpeakerDecision(f"spk_{len(self.centroids):02d}",
                               "confirmed_new", candidate_similarity)

    def _update_cluster(self, index: int, embedding: list[float]) -> None:
        count = self.counts[index]
        self.centroids[index] = self._weighted_centroid(
            self.centroids[index], count, embedding, 1)
        self.counts[index] = count + 1

    def _merge_confirmed_clusters(self) -> None:
        while True:
            best_pair = None
            best_similarity = self.merge_threshold
            for left in range(len(self.centroids) - 1):
                if self._canonical_index(left) != left:
                    continue
                for right in range(left + 1, len(self.centroids)):
                    if self._canonical_index(right) != right:
                        continue
                    similarity = self._cosine(self.centroids[left], self.centroids[right])
                    if similarity >= best_similarity:
                        best_pair = (left, right)
                        best_similarity = similarity
            if best_pair is None:
                return
            left, right = best_pair
            self.centroids[left] = self._weighted_centroid(
                self.centroids[left], self.counts[left],
                self.centroids[right], self.counts[right])
            self.counts[left] += self.counts[right]
            self.aliases[right] = left
            self.merge_count += 1

    def _expire_candidates(self) -> None:
        cutoff = self.segment_count - self.candidate_ttl
        self.candidates = [item for item in self.candidates if item["last_seen"] > cutoff]

    def _canonical_index(self, index: int) -> int:
        trail = []
        while index in self.aliases:
            trail.append(index)
            index = self.aliases[index]
        for old_index in trail:
            self.aliases[old_index] = index
        return index

    def _canonical_speaker_id(self, speaker_id: str) -> str:
        index = max(0, int(speaker_id.rsplit("_", 1)[1]) - 1)
        return f"spk_{self._canonical_index(index) + 1:02d}"

    @classmethod
    def _weighted_centroid(cls, left: list[float], left_weight: int,
                           right: list[float], right_weight: int) -> list[float]:
        total = left_weight + right_weight
        return cls._normalize([
            (a * left_weight + b * right_weight) / total for a, b in zip(left, right)
        ])

    def summary(self) -> list[dict[str, Any]]:
        consolidated: dict[str, int] = {}
        for speaker_id, count in self.assigned_counts.items():
            canonical = self._canonical_speaker_id(speaker_id)
            consolidated[canonical] = consolidated.get(canonical, 0) + count
        return [
            {"speaker_id": speaker_id, "label": self.label(speaker_id), "segment_count": count}
            for speaker_id, count in sorted(consolidated.items())
        ]

    def diagnostics(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "attempted_segments": self.segment_count,
            "labeled_segments": sum(self.assigned_counts.values()),
            "candidate_segments": self.candidate_count,
            "active_candidates": len(self.candidates),
            "confirmed_speakers": sum(
                1 for index in range(len(self.centroids)) if self._canonical_index(index) == index),
            "merged_clusters": self.merge_count,
            "skipped_short_segments": self.skipped_short_count,
            "skipped_quiet_segments": self.skipped_quiet_count,
            "failed_segments": self.failed_count,
            "last_error": self.last_error or None,
        }

    @staticmethod
    def label(speaker_id: str | None) -> str | None:
        if not speaker_id:
            return None
        try:
            return f"说话人 {int(speaker_id.rsplit('_', 1)[1])}"
        except (ValueError, IndexError):
            return speaker_id

    @staticmethod
    def _cosine(left: list[float], right: list[float]) -> float:
        return sum(a * b for a, b in zip(left, right))

    @staticmethod
    def _normalize(vector: list[float]) -> list[float]:
        norm = math.sqrt(sum(value * value for value in vector))
        if norm <= 1e-12:
            raise ValueError("zero speaker embedding")
        return [value / norm for value in vector]


async def probe_speaker_backend() -> dict[str, Any]:
    """Check the real CAM++ dependency used by device and browser meetings."""
    mode = os.getenv("SPEAKER_MODE", "off").strip().lower()
    if mode == "off":
        return {"mode": mode, "required": False, "ready": True, "detail": "disabled"}
    if mode == "mock":
        return {"mode": mode, "required": False, "ready": True, "detail": "mock"}
    embedding_url = os.getenv("SPEAKER_EMBEDDING_URL", "http://speaker:10100/embed")
    health_url = embedding_url.rsplit("/", 1)[0] + "/health"
    timeout = min(5.0, max(0.5, float(os.getenv("SPEAKER_HEALTH_TIMEOUT_SECONDS", "2"))))
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            response = await client.get(health_url)
            response.raise_for_status()
            payload = response.json()
        return {
            "mode": mode,
            "required": True,
            "ready": payload.get("status") == "ok",
            "detail": payload.get("model") or "CAM++",
        }
    except Exception as exc:
        logger.error("speaker_backend_unavailable url=%s error=%s", health_url, exc)
        return {
            "mode": mode,
            "required": True,
            "ready": False,
            "detail": type(exc).__name__,
        }
