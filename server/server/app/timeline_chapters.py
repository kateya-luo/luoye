"""Canonical meeting timeline chapters derived from ASR segments and LLM topics.

The LLM proposes semantic chapter boundaries.  This module owns validation,
rolling freeze semantics and MARK attribution so clients never have to invent
timestamps or associate marks themselves.
"""
from __future__ import annotations

from typing import Any


TIMELINE_SCHEMA = 3
MIN_SEMANTIC_BOUNDARY_GAP_MS = 10_000
SEMANTIC_BOUNDARY_CONFIDENCE = 0.72
SEMANTIC_BOUNDARY_KINDS = {
    "topic_change", "goal_change", "decision_phase", "phase_change",
}


def _text(value: Any, limit: int) -> str:
    return str(value or "").strip()[:limit]


def transcript_with_time_and_marks(segments: list[dict[str, Any]],
                                   marks: list[dict[str, Any]]) -> str:
    """LLM input with stable anchors, real media time and MARK emphasis."""
    valid = [segment for segment in segments if _text(segment.get("text"), 1)]
    marked_ids: set[int] = set()
    for mark in marks:
        if not valid:
            break
        at_ms = int(mark.get("at_ms") or 0)
        nearest = min(range(len(valid)), key=lambda index: abs(
            int(valid[index].get("start_ms") or 0) - at_ms))
        marked_ids.add(nearest)
    lines = []
    for index, item in enumerate(valid):
        prefix = f"[anchor=S{index + 1:04d} t={int(item.get('start_ms') or 0)}ms]"
        if index in marked_ids:
            prefix += " [MARK: user-designated key point]"
        speech = (f"[{item['speaker_label']}] {item['text']}"
                  if item.get("speaker_label") else str(item.get("text") or ""))
        lines.append(f"{prefix} {speech}")
    return "\n".join(lines)


def _canonical_segment_index(item: dict[str, Any], segments: list[dict[str, Any]], *,
                             anchor_required: bool) -> tuple[int | None, str]:
    """Resolve an LLM boundary to a real ASR segment, never to invented milliseconds."""
    if not segments:
        return 0, "none"
    anchor = str(item.get("anchor") or "").strip().upper()
    if anchor.startswith("S") and anchor[1:].isdigit():
        anchored = int(anchor[1:]) - 1
        if 0 <= anchored < len(segments):
            return anchored, "anchor"
    if anchor_required:
        return None, "invalid_anchor"

    query_start = int(item.get("start_ms") or 0)
    nearest = min(range(len(segments)), key=lambda index: abs(
        int(segments[index].get("start_ms") or 0) - query_start))
    return nearest, "nearest"


def _candidate_chapters(result: dict[str, Any], segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    raw = result.get("timeline_chapters")
    chapters: list[dict[str, Any]] = []
    if isinstance(raw, list):
        raw_items = [item for item in raw if isinstance(item, dict)]
        anchor_required = int(result.get("timeline_anchor_protocol") or 0) >= 1
        for index, item in enumerate(raw_items):
            if not isinstance(item, dict):
                continue
            title = _text(item.get("title"), 80)
            points = [_text(point, 180) for point in (item.get("items") or [])
                      if _text(point, 180)][:5]
            try:
                proposed_start_ms = max(0, int(item.get("start_ms") or 0))
                end_ms = max(proposed_start_ms, int(item.get("end_ms") or proposed_start_ms))
            except (TypeError, ValueError):
                continue
            if title and points:
                segment_index, timestamp_source = _canonical_segment_index(
                    {**item, "title": title, "items": points}, segments,
                    anchor_required=anchor_required)
                if segment_index is None:
                    continue
                start_ms = (int(segments[segment_index].get("start_ms") or 0)
                            if segments else proposed_start_ms)
                raw_boundary = item.get("boundary")
                boundary = dict(raw_boundary) if isinstance(raw_boundary, dict) else {}
                kind = _text(boundary.get("kind"), 32).lower()
                if not kind:
                    kind = "initial" if index == 0 else "unspecified"
                try:
                    confidence = min(1.0, max(0.0, float(boundary.get("confidence") or 0.0)))
                except (TypeError, ValueError):
                    confidence = 0.0
                if index == 0 and kind == "initial" and confidence <= 0.0:
                    confidence = 1.0
                chapters.append({"start_ms": start_ms, "end_ms": end_ms,
                                 "title": title, "items": points,
                                 "anchor": str(item.get("anchor") or "").strip().upper(),
                                 "timestamp_source": timestamp_source,
                                 "boundary": {
                                     "kind": kind,
                                     "confidence": confidence,
                                     "reason": _text(boundary.get("reason"), 180),
                                 }})
    chapters.sort(key=lambda item: (item["start_ms"], item["end_ms"]))
    if chapters:
        return chapters

    # Compatibility fallback for old/mock LLM responses.  A missing semantic
    # timeline must never be converted into evenly-spaced fake chapters.
    # Keep one open chapter and let later semantic evidence create boundaries.
    branches = ((result.get("mindmap") or {}).get("branches") or [])
    if not segments:
        return []
    points: list[str] = []
    for branch in branches:
        if not isinstance(branch, dict):
            continue
        for point in branch.get("items") or []:
            clean = _text(point, 180)
            if clean and clean not in points:
                points.append(clean)
            if len(points) >= 5:
                break
        if len(points) >= 5:
            break
    if not points:
        points = [_text(item.get("text"), 180) for item in segments[-2:]
                  if _text(item.get("text"), 180)]
    title = _text((result.get("mindmap") or {}).get("title"), 80) or "会议进行中"
    return [{"start_ms": int(segments[0].get("start_ms") or 0),
             "end_ms": int(segments[-1].get("end_ms") or 0),
             "title": title, "items": points[:5],
             "boundary": {"kind": "initial", "confidence": 1.0,
                          "reason": "compatibility fallback"}}]


def _mark_near(start_ms: int, marks: list[dict[str, Any]]) -> bool:
    return any(abs(int(mark.get("at_ms") or 0) - start_ms) <= 10_000 for mark in marks)


def _accept_boundary(candidate: dict[str, Any], active_start_ms: int,
                     marks: list[dict[str, Any]]) -> bool:
    """Server-side adjudication for one model-proposed semantic boundary."""
    start_ms = int(candidate.get("start_ms") or 0)
    boundary = candidate.get("boundary") or {}
    kind = str(boundary.get("kind") or "unspecified")
    confidence = float(boundary.get("confidence") or 0.0)
    has_mark = _mark_near(start_ms, marks)
    if kind == "mark":
        return has_mark
    if start_ms - active_start_ms < MIN_SEMANTIC_BOUNDARY_GAP_MS:
        return False
    if kind in SEMANTIC_BOUNDARY_KINDS:
        return confidence >= SEMANTIC_BOUNDARY_CONFIDENCE
    return False


def _merge_into_current(current: dict[str, Any], proposed: dict[str, Any], *,
                        replace: bool = False) -> dict[str, Any]:
    """Fold same-topic details into the mutable tail without adding a timestamp."""
    merged = dict(current)
    if replace:
        merged["title"] = proposed["title"]
        merged["items"] = list(proposed["items"])
    else:
        items = list(merged.get("items") or [])
        for point in proposed.get("items") or []:
            if point not in items:
                items.append(point)
        merged["items"] = items[:5]
    merged["end_ms"] = max(int(merged.get("end_ms") or 0),
                           int(proposed.get("end_ms") or 0))
    return merged


def _semantic_tail(candidates: list[dict[str, Any]], marks: list[dict[str, Any]], *,
                   seed: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    if not candidates:
        return [dict(seed)] if seed else []
    chapters: list[dict[str, Any]] = []
    remaining = list(candidates)
    if seed is None:
        current = dict(remaining.pop(0))
    else:
        current = dict(seed)
        if remaining and int(remaining[0]["start_ms"]) <= int(current["start_ms"]):
            current = _merge_into_current(current, remaining.pop(0), replace=True)
    for candidate in remaining:
        if int(candidate["start_ms"]) <= int(current["start_ms"]):
            current = _merge_into_current(current, candidate, replace=True)
            continue
        if _accept_boundary(candidate, int(current["start_ms"]), marks):
            current["end_ms"] = max(int(current["start_ms"]),
                                    int(candidate["start_ms"]) - 1)
            chapters.append(current)
            current = dict(candidate)
        else:
            current = _merge_into_current(current, candidate)
    chapters.append(current)
    return chapters


def _with_marks(chapters: list[dict[str, Any]], marks: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for index, chapter in enumerate(chapters):
        start = int(chapter["start_ms"])
        end = int(chapter["end_ms"])
        next_start = (int(chapters[index + 1]["start_ms"])
                      if index + 1 < len(chapters) else None)
        chapter_marks = []
        for mark in marks:
            at_ms = int(mark.get("at_ms") or 0)
            if at_ms < start:
                continue
            if next_start is not None and at_ms >= next_start:
                continue
            if next_start is None and at_ms > max(end, start) + 60_000:
                continue
            chapter_marks.append({"id": str(mark.get("id") or ""), "at_ms": at_ms,
                                  "kind": str(mark.get("kind") or "mark"),
                                  "label": mark.get("label")})
        chapter["marks"] = chapter_marks
        chapter["mark_count"] = len(chapter_marks)
    return chapters


def build_timeline_chapters(result: dict[str, Any], segments: list[dict[str, Any]],
                            marks: list[dict[str, Any]], *, rolling: bool,
                            previous: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    segments = [segment for segment in segments if _text(segment.get("text"), 1)]
    candidates = _candidate_chapters(result, segments)
    if not candidates:
        return []
    audio_end = max((int(segment.get("end_ms") or segment.get("start_ms") or 0)
                     for segment in segments), default=0)
    for index, chapter in enumerate(candidates):
        if index + 1 < len(candidates):
            chapter["end_ms"] = max(chapter["start_ms"], candidates[index + 1]["start_ms"] - 1)
        else:
            chapter["end_ms"] = max(chapter["end_ms"], audio_end)

    if rolling and previous:
        frozen = [dict(item) for item in previous if item.get("status") == "frozen"]
        prior_current = next((dict(item) for item in reversed(previous)
                              if item.get("status") == "current"), None)
        boundary = (int(frozen[-1].get("end_ms") or frozen[-1].get("start_ms") or 0)
                    if frozen else -1)
        tail = [item for item in candidates if item["end_ms"] > boundary]
        if frozen and tail and tail[0]["start_ms"] <= boundary:
            tail[0]["start_ms"] = boundary + 1
        chapters = frozen + _semantic_tail(tail, marks, seed=prior_current)
    else:
        chapters = _semantic_tail(candidates, marks)

    # A rolling timeline has exactly one mutable tail.  Once a later chapter is
    # confirmed, every earlier chapter is frozen and remains stable thereafter.
    for index, chapter in enumerate(chapters):
        chapter["chapter_no"] = index + 1
        chapter["status"] = "current" if rolling and index == len(chapters) - 1 else "frozen"
    return _with_marks(chapters, marks)


def enrich_summary_timeline(storage: Any, session_id: str, result: dict[str, Any], *,
                            rolling: bool) -> dict[str, Any]:
    enriched = dict(result)
    meeting = storage.get_meeting(session_id) or {}
    previous_summary = meeting.get("summary") or {}
    previous = (previous_summary.get("timeline_chapters") or [])
    if int(previous_summary.get("timeline_schema") or 0) < TIMELINE_SCHEMA:
        previous = []
    enriched["timeline_chapters"] = build_timeline_chapters(
        enriched, meeting.get("segments") or storage.load_segments(session_id),
        meeting.get("marks") or [], rolling=rolling, previous=previous)
    enriched["timeline_schema"] = TIMELINE_SCHEMA
    return enriched
