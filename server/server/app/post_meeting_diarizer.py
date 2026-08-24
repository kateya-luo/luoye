import logging
import math
import os
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import httpx


logger = logging.getLogger("ai_recorder.post_meeting_diarizer")


class PostMeetingDiarizer:
    """Globally reconcile real-time speaker labels after a meeting ends."""

    sample_rate = 16000
    sample_width = 2

    def __init__(self):
        self.enabled = os.getenv("POST_MEETING_DIARIZATION_ENABLED", "true").strip().lower() in {
            "1", "true", "yes", "on",
        }
        self.mode = os.getenv("SPEAKER_MODE", "off").strip().lower()
        self.url = os.getenv("SPEAKER_EMBEDDING_URL", "http://speaker:10100/embed")
        self.threshold = float(os.getenv(
            "POST_MEETING_SPEAKER_THRESHOLD",
            os.getenv("SPEAKER_SIMILARITY_THRESHOLD", "0.68"),
        ))
        self.min_seconds = float(os.getenv("SPEAKER_MIN_SEGMENT_SECONDS", "2.0"))
        self.max_segment_seconds = float(os.getenv("POST_MEETING_MAX_SEGMENT_SECONDS", "12"))
        self.max_embeddings = max(8, int(os.getenv("POST_MEETING_MAX_EMBEDDINGS", "300")))
        self.request_timeout = float(os.getenv("SPEAKER_REQUEST_TIMEOUT_SECONDS", "20"))

    async def correct(self, audio_path: Path, segments: list[dict[str, Any]]) -> dict[str, Any]:
        if not self.enabled:
            return self._unchanged(segments, "disabled")
        if self.mode != "remote":
            return self._unchanged(segments, "skipped", f"speaker mode is {self.mode}")
        if len(segments) < 2 or not audio_path.exists():
            return self._unchanged(segments, "skipped", "not enough segments or audio is missing")

        candidates = self._select_candidates(segments)
        if len(candidates) < 2:
            return self._unchanged(segments, "skipped", "not enough voiceprint-quality segments")

        embedded: list[tuple[int, list[float]]] = []
        failures = 0
        timeout = httpx.Timeout(self.request_timeout)
        async with httpx.AsyncClient(timeout=timeout) as client:
            with audio_path.open("rb") as audio:
                for segment_index in candidates:
                    pcm = self._read_segment(audio, segments[segment_index])
                    if not pcm:
                        failures += 1
                        continue
                    try:
                        embedding = await self._fetch_embedding(client, pcm)
                        embedded.append((segment_index, embedding))
                    except Exception as exc:
                        failures += 1
                        logger.warning(
                            "post_meeting_embedding_failed segment=%d bytes=%d error=%s",
                            segment_index, len(pcm), exc,
                        )

        if len(embedded) < 2:
            return self._unchanged(segments, "failed", "speaker embeddings could not be generated")

        assignments = self._global_cluster(embedded)
        corrected = [dict(segment) for segment in segments]
        for segment_index, cluster_index in assignments.items():
            corrected[segment_index]["post_meeting_speaker_cluster"] = cluster_index

        self._fill_unembedded_assignments(corrected, assignments)
        cluster_order = self._stable_cluster_order(corrected)
        changes = 0
        counts: Counter[str] = Counter()
        transcript: list[str] = []
        for segment in corrected:
            cluster_index = segment.pop("post_meeting_speaker_cluster", None)
            if cluster_index is None:
                speaker_id = segment.get("speaker_id")
            else:
                speaker_id = f"spk_{cluster_order[cluster_index]:02d}"
            old_speaker_id = segment.get("speaker_id")
            if old_speaker_id and speaker_id and old_speaker_id != speaker_id:
                changes += 1
            if old_speaker_id and "realtime_speaker_id" not in segment:
                segment["realtime_speaker_id"] = old_speaker_id
            segment["speaker_id"] = speaker_id
            segment["speaker_label"] = self.label(speaker_id)
            if speaker_id:
                counts[speaker_id] += 1
            text = str(segment.get("text") or "").strip()
            if text:
                transcript.append(f"[{self.label(speaker_id)}] {text}" if speaker_id else text)

        speakers = [
            {"speaker_id": speaker_id, "label": self.label(speaker_id), "segment_count": count}
            for speaker_id, count in sorted(counts.items())
        ]
        logger.info(
            "post_meeting_diarization_complete segments=%d embedded=%d failures=%d speakers=%d changes=%d",
            len(segments), len(embedded), failures, len(speakers), changes,
        )
        return {
            "status": "corrected",
            "segments": corrected,
            "transcript": transcript,
            "speakers": speakers,
            "embedded_segments": len(embedded),
            "failed_embeddings": failures,
            "speaker_count": len(speakers),
            "changed_segments": changes,
        }

    def _select_candidates(self, segments: list[dict[str, Any]]) -> list[int]:
        eligible = []
        for index, segment in enumerate(segments):
            duration = max(0, int(segment.get("end_ms") or 0) - int(segment.get("start_ms") or 0))
            if duration >= self.min_seconds * 1000:
                eligible.append((index, duration))
        if len(eligible) <= self.max_embeddings:
            return [index for index, _ in eligible]

        # Prefer clear, long utterances, but reserve half of the budget for temporal coverage.
        quality_budget = self.max_embeddings // 2
        quality = {index for index, _ in sorted(eligible, key=lambda item: item[1], reverse=True)[:quality_budget]}
        remaining_budget = self.max_embeddings - len(quality)
        step = len(eligible) / remaining_budget
        coverage = {eligible[min(len(eligible) - 1, math.floor(offset * step))][0] for offset in range(remaining_budget)}
        selected = quality | coverage
        if len(selected) < self.max_embeddings:
            for index, _ in sorted(eligible, key=lambda item: item[1], reverse=True):
                selected.add(index)
                if len(selected) >= self.max_embeddings:
                    break
        return sorted(selected)

    def _read_segment(self, audio, segment: dict[str, Any]) -> bytes:
        start_ms = max(0, int(segment.get("start_ms") or 0))
        end_ms = max(start_ms, int(segment.get("end_ms") or start_ms))
        end_ms = min(end_ms, start_ms + round(self.max_segment_seconds * 1000))
        start_byte = self._aligned_byte(start_ms)
        end_byte = self._aligned_byte(end_ms)
        if end_byte <= start_byte:
            return b""
        audio.seek(start_byte)
        return audio.read(end_byte - start_byte)

    def _aligned_byte(self, milliseconds: int) -> int:
        value = round(milliseconds * self.sample_rate * self.sample_width / 1000)
        return value - (value % self.sample_width)

    async def _fetch_embedding(self, client: httpx.AsyncClient, pcm: bytes) -> list[float]:
        response = await client.post(
            self.url,
            content=pcm,
            headers={
                "Content-Type": "application/octet-stream",
                "X-Audio-Format": "pcm_s16le",
                "X-Sample-Rate": str(self.sample_rate),
            },
        )
        response.raise_for_status()
        vector = response.json().get("embedding")
        if not isinstance(vector, list) or not vector:
            raise ValueError("speaker service returned no embedding")
        return self._normalize([float(value) for value in vector])

    def _global_cluster(self, embedded: list[tuple[int, list[float]]]) -> dict[int, int]:
        # First pass grows dynamically over the complete meeting sample; meeting
        # size is inferred from evidence and is never clipped to a configured K.
        # A second centroid-merge pass then repairs fragmented clusters globally.
        clusters = []
        for segment_index, embedding in embedded:
            if not clusters:
                clusters.append({"members": [segment_index], "centroid": embedding, "weight": 1})
                continue
            similarities = [self._cosine(embedding, cluster["centroid"]) for cluster in clusters]
            best = max(range(len(similarities)), key=similarities.__getitem__)
            if similarities[best] < self.threshold:
                clusters.append({"members": [segment_index], "centroid": embedding, "weight": 1})
                continue
            cluster = clusters[best]
            total_weight = cluster["weight"] + 1
            cluster["centroid"] = self._normalize([
                (a * cluster["weight"] + b) / total_weight
                for a, b in zip(cluster["centroid"], embedding)
            ])
            cluster["weight"] = total_weight
            cluster["members"].append(segment_index)

        while len(clusters) > 1:
            best_pair = None
            best_similarity = -1.0
            for left in range(len(clusters) - 1):
                for right in range(left + 1, len(clusters)):
                    similarity = self._cosine(clusters[left]["centroid"], clusters[right]["centroid"])
                    if similarity > best_similarity:
                        best_similarity = similarity
                        best_pair = (left, right)
            if best_pair is None or best_similarity < self.threshold:
                break
            left, right = best_pair
            first, second = clusters[left], clusters[right]
            total_weight = first["weight"] + second["weight"]
            centroid = self._normalize([
                (a * first["weight"] + b * second["weight"]) / total_weight
                for a, b in zip(first["centroid"], second["centroid"])
            ])
            first["members"].extend(second["members"])
            first["centroid"] = centroid
            first["weight"] = total_weight
            clusters.pop(right)
        return {
            segment_index: cluster_index
            for cluster_index, cluster in enumerate(clusters)
            for segment_index in cluster["members"]
        }

    @staticmethod
    def _fill_unembedded_assignments(segments: list[dict[str, Any]], assignments: dict[int, int]):
        original_votes: dict[str, Counter[int]] = defaultdict(Counter)
        for index, cluster_index in assignments.items():
            original = segments[index].get("speaker_id")
            if original:
                original_votes[str(original)][cluster_index] += 1
        original_map = {
            original: votes.most_common(1)[0][0]
            for original, votes in original_votes.items() if votes
        }
        previous_cluster = None
        for index, segment in enumerate(segments):
            if index in assignments:
                previous_cluster = assignments[index]
                continue
            original = str(segment.get("speaker_id") or "")
            cluster_index = original_map.get(original, previous_cluster)
            if cluster_index is not None:
                segment["post_meeting_speaker_cluster"] = cluster_index
                previous_cluster = cluster_index

    @staticmethod
    def _stable_cluster_order(segments: list[dict[str, Any]]) -> dict[int, int]:
        first_seen = []
        for segment in segments:
            cluster_index = segment.get("post_meeting_speaker_cluster")
            if cluster_index is not None and cluster_index not in first_seen:
                first_seen.append(cluster_index)
        return {cluster_index: order for order, cluster_index in enumerate(first_seen, start=1)}

    @staticmethod
    def _unchanged(segments: list[dict[str, Any]], status: str, reason: str | None = None) -> dict[str, Any]:
        counts = Counter(str(segment.get("speaker_id")) for segment in segments if segment.get("speaker_id"))
        speakers = [
            {"speaker_id": speaker_id, "label": PostMeetingDiarizer.label(speaker_id), "segment_count": count}
            for speaker_id, count in sorted(counts.items())
        ]
        transcript = []
        for segment in segments:
            text = str(segment.get("text") or "").strip()
            speaker_id = segment.get("speaker_id")
            if text:
                transcript.append(f"[{PostMeetingDiarizer.label(speaker_id)}] {text}" if speaker_id else text)
        return {
            "status": status,
            "reason": reason,
            "segments": [dict(segment) for segment in segments],
            "transcript": transcript,
            "speakers": speakers,
            "embedded_segments": 0,
            "failed_embeddings": 0,
            "speaker_count": len(speakers),
            "changed_segments": 0,
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
