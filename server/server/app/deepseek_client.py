import json
import logging
import os
import re
from typing import Any

import httpx

logger = logging.getLogger("ai_recorder.deepseek")


class DeepSeekClient:
    def __init__(self):
        self.key = os.getenv("DEEPSEEK_API_KEY", "")
        self.base = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com").rstrip("/")

    async def summarize(self, transcript: str, *, rolling: bool = False,
                        source_language: str = "auto", output_language: str = "auto") -> dict[str, Any]:
        transcript = transcript.strip()
        if not transcript:
            return self._empty_result(output_language)
        if not self.key:
            title = "Meeting highlights" if output_language == "en" else "会议重点"
            return {
                "summary": transcript,
                "decisions": [],
                "action_items": [],
                "mindmap": {"title": title, "branches": []},
            }

        stage = "The meeting is ongoing; produce an interim summary." if rolling else "The meeting has ended; produce the final minutes."
        language_instruction = {
            "zh": "Write every generated field in Simplified Chinese.",
            "en": "Write every generated field in English.",
            "auto": "Use the transcript's predominant language. Preserve names and technical terms in their original language.",
        }.get(output_language, "Use the transcript's predominant language.")
        system_prompt = f"""You are a precise meeting secretary. {stage}
The source language mode is {source_language}. {language_instruction}
Transcript lines may start with a meeting-local label such as [说话人 1]. Preserve these labels when attributing viewpoints, decisions, and action owners; do not guess real names.
Return only one JSON object, without Markdown or explanation, in this exact shape:
{{
  "summary": "concise, faithful summary",
  "decisions": ["explicit decision"],
  "action_items": [{{"task": "task", "assignee": "person or to be confirmed", "deadline": "deadline or to be confirmed"}}],
  "mindmap": {{
    "title": "short meeting topic",
    "branches": [
      {{"title": "major theme", "items": ["fact, conclusion, risk, or next step"]}}
    ]
  }},
  "timeline_chapters": [
    {{
      "anchor": "S0001",
      "start_ms": 0,
      "end_ms": 0,
      "title": "short topic",
      "items": ["key fact or conclusion"],
      "boundary": {{
        "kind": "initial|topic_change|goal_change|decision_phase|phase_change|mark",
        "confidence": 0.0,
        "reason": "short evidence for starting this chapter"
      }}
    }}
  ]
}}
The mind map must contain at most 6 branches and at most 5 concise items per branch. Merge duplicate topics.
For every timeline chapter, copy the exact [anchor=S0001] label from the transcript line where that chapter begins. Never invent an anchor and never calculate timestamps yourself. start_ms may echo that line's t value, but the server treats anchor as authoritative. The first chapter uses the first transcript anchor, boundary.kind="initial" and confidence=1.0.
Create a later chapter ONLY when the meeting clearly changes topic, objective, decision-making phase, workflow phase, or when a user MARK establishes a distinct new focus. Use the matching boundary.kind and report an honest confidence from 0.0 to 1.0 plus concise transcript evidence in boundary.reason.
Elapsed time, silence, a speaker change, another example, or incremental detail within the same topic is NEVER a chapter boundary. Update the open chapter's title/items instead. Do not target a fixed interval or a fixed number of chapters; a long unchanged discussion may remain one chapter for many minutes.
Keep chronological order, use 1-5 concise items per chapter according to the actual information available, and keep exactly one ongoing final chapter open at the latest timestamp. A genuinely clear change may be reported soon after the prior chapter, but avoid unstable rapid boundaries.
Lines tagged [MARK: user-designated key point] deserve special attention. Use boundary.kind="mark" only when the marked passage begins a distinct focus; otherwise preserve its nearby fact in the current chapter. MARK does not make an unsupported statement a decision.
Never invent a name, date, decision, or task that does not appear in the transcript."""
        async with httpx.AsyncClient(timeout=60) as client:
            response = await client.post(
                f"{self.base}/chat/completions",
                headers={"Authorization": f"Bearer {self.key}"},
                json={"model": "deepseek-chat", "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": system_prompt},
                                   {"role": "user", "content": transcript}], "temperature": 0.2},
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return self._parse_result(content, output_language)

    async def translate(self, text: str, target_lang: str = "zh",
                        context: list[tuple[str, str]] | None = None) -> str:
        """实时听译（v2 翻译管道）：把一句转写翻成目标语言。
        context=最近几对(原文,译文)，用于术语/代词一致（由 SessionTranslator 维护滚动窗口）。
        无 key 时回显原文（链路可测）；失败也回显原文，绝不阻断字幕链。"""
        text = (text or "").strip()
        if not text:
            return ""
        if not self.key:
            return text
        target = {"zh": "Simplified Chinese", "en": "English", "ja": "Japanese",
                  "ko": "Korean", "fr": "French", "de": "German"}.get(target_lang, target_lang)
        system = (f"You are a professional simultaneous interpreter. Translate the utterance into {target}. "
                  "Output ONLY the translation itself — no quotes, no notes, no original text, no explanations. "
                  "The utterance comes from streaming ASR and may contain recognition noise; translate the intended meaning.")
        if context:
            ctx = "\n".join(f"- {s} => {t}" for s, t in context[-3:])
            system += ("\nRecent sentences already translated (keep terminology and pronouns consistent"
                       " with them; translate ONLY the new utterance):\n" + ctx)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.post(
                    f"{self.base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.key}"},
                    json={"model": "deepseek-chat",
                          "messages": [{"role": "system", "content": system},
                                       {"role": "user", "content": text}], "temperature": 0.2},
                )
                resp.raise_for_status()
                return (resp.json()["choices"][0]["message"]["content"] or "").strip() or text
        except Exception:
            logger.exception("deepseek_translate_failed")
            return text

    async def extract_agenda(self, text: str, now_iso: str, timezone_name: str) -> dict[str, Any] | None:
        """从 MARK 语音中抽取议程；无 key/调用失败返回 None，由确定性口语解析器兜底。"""
        if not self.key or not text.strip():
            return None
        prompt = f"""Extract one calendar item from the utterance. Current time is {now_iso}, timezone {timezone_name}.
Return only JSON: {{"title":"...","type":"meeting|class|todo|reminder","start":"ISO-8601 with offset","end":null}}.
Resolve relative words such as today/tomorrow/今晚. Never invent a time; if no explicit time return {{"error":"missing_time"}}."""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{self.base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.key}"},
                    json={"model": "deepseek-chat", "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": prompt},
                                       {"role": "user", "content": text}], "temperature": 0},
                )
                response.raise_for_status()
                payload = json.loads(response.json()["choices"][0]["message"]["content"])
            return payload if isinstance(payload, dict) and not payload.get("error") else None
        except Exception:
            logger.exception("deepseek_agenda_extract_failed")
            return None

    async def extract_todo(self, text: str, now_iso: str,
                           timezone_name: str) -> dict[str, Any] | None:
        """Split one spoken command into actionable content and optional time.

        No key or a failed/invalid model response returns ``None`` so the
        deterministic parser can still create the todo without losing speech.
        """
        text = (text or "").strip()
        if not self.key or not text:
            return None
        prompt = f"""You parse one Chinese voice command into exactly one actionable todo.
Current time: {now_iso}. Timezone: {timezone_name}.
Return ONLY one JSON object, no Markdown, with this exact shape:
{{
  "content": "concise action content without conversational filler",
  "has_time": true,
  "due_at": "ISO-8601 timestamp with UTC offset or null",
  "time_text": "short resolved Chinese time or 未定时间",
  "type": "todo|reminder|meeting|class",
  "confidence": 0.0
}}
Rules:
- The utterance contains two independent facts: optional time and required action content.
- Remove command wrappers such as “提醒我”, “帮我记一下”, “有个事情”, “之后要记得”, “我要做”.
- Preserve names, materials, quantities and the concrete action; never summarize away the task.
- Resolve explicit relative dates from Current time. Period defaults are 上午=09:00, 中午=12:00, 下午=15:00, 晚上=19:00.
- “之后”, “以后”, “有空”, “记一下” alone are NOT explicit times: set has_time=false, due_at=null, time_text="未定时间".
- If any explicit time exists, has_time=true and due_at must contain an offset. Otherwise never invent a date.
- If the utterance has no actionable content, return content="" and confidence=0."""
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.post(
                    f"{self.base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.key}"},
                    json={"model": "deepseek-chat", "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": prompt},
                                       {"role": "user", "content": text}], "temperature": 0},
                )
                response.raise_for_status()
                payload = json.loads(response.json()["choices"][0]["message"]["content"])
            if not isinstance(payload, dict) or not str(payload.get("content") or "").strip():
                return None
            return payload
        except Exception:
            logger.exception("deepseek_todo_extract_failed")
            return None

    async def merge_gap(self, current: dict[str, Any], gap_text: str, *,
                        source_language: str = "auto", output_language: str = "auto") -> dict[str, Any]:
        """把断网补录段"适当总结插入"现有纪要（增量小调用，不重发全文转写）。
        current: 当前纪要 JSON；gap_text: 补录段转写文本。失败/无 key 时做保底拼接，绝不丢内容。"""
        gap_text = (gap_text or "").strip()
        if not gap_text:
            return current
        base = {"summary": "", "decisions": [], "action_items": [],
                "mindmap": {"title": "会议重点", "branches": []}, **(current or {})}
        if not self.key:
            tag = "[Recovered] " if output_language == "en" else "【补录】"
            base["summary"] = (base["summary"] + "\n" + tag + gap_text).strip()
            return base
        language_instruction = {
            "zh": "Write every generated field in Simplified Chinese.",
            "en": "Write every generated field in English.",
            "auto": "Use the minutes' predominant language. Preserve names and technical terms in their original language.",
        }.get(output_language, "Use the minutes' predominant language.")
        system_prompt = f"""You are a precise meeting secretary. A portion of the meeting audio was lost to a network outage, has now been recovered and transcribed, and must be woven into the existing minutes.
The source language mode is {source_language}. {language_instruction}
Rules:
- Integrate the recovered content into "summary" at the appropriate place; keep unrelated wording as stable as possible.
- Append to "decisions" / "action_items" / "mindmap" ONLY what the recovered text clearly adds; never remove or rewrite existing items unless directly contradicted.
- Never invent a name, date, decision, or task that does not appear in the input.
Return only one JSON object in exactly the same shape as "current_minutes" (keys: summary, decisions, action_items, mindmap)."""
        user_payload = json.dumps({"current_minutes": {k: base[k] for k in ("summary", "decisions", "action_items", "mindmap")},
                                   "recovered_segment": gap_text}, ensure_ascii=False)
        try:
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(
                    f"{self.base}/chat/completions",
                    headers={"Authorization": f"Bearer {self.key}"},
                    json={"model": "deepseek-chat", "response_format": {"type": "json_object"},
                          "messages": [{"role": "system", "content": system_prompt},
                                       {"role": "user", "content": user_payload}], "temperature": 0.2},
                )
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
            merged = self._parse_result(content, output_language)
        except Exception:
            logger.exception("deepseek_merge_gap_failed")
            tag = "[Recovered] " if output_language == "en" else "【补录】"
            base["summary"] = (base["summary"] + "\n" + tag + gap_text).strip()
            return base
        # 保留 current 里模型 shape 之外的字段（speakers / source_language 等）
        for k, v in base.items():
            merged.setdefault(k, v)
        return merged

    @staticmethod
    def _empty_result(output_language: str = "auto") -> dict[str, Any]:
        summary = "No valid final transcript was received." if output_language == "en" else "暂未收到有效的最终转写内容。"
        title = "Meeting highlights" if output_language == "en" else "会议重点"
        return {"summary": summary, "decisions": [], "action_items": [],
                "mindmap": {"title": title, "branches": []}}

    @classmethod
    def _parse_result(cls, content: str, output_language: str = "auto") -> dict[str, Any]:
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.I)
        try:
            payload = json.loads(cleaned)
        except json.JSONDecodeError:
            logger.warning("deepseek_non_json_response")
            title = "Meeting highlights" if output_language == "en" else "会议重点"
            return {"summary": cleaned, "decisions": [], "action_items": [],
                    "mindmap": {"title": title, "branches": []}}
        fallback = "To be confirmed" if output_language == "en" else "待确认"
        decisions = payload.get("decisions") if isinstance(payload.get("decisions"), list) else []
        raw_actions = payload.get("action_items") if isinstance(payload.get("action_items"), list) else []
        actions = []
        for item in raw_actions:
            if isinstance(item, str):
                actions.append({"task": item, "assignee": fallback, "deadline": fallback})
            elif isinstance(item, dict):
                actions.append({"task": str(item.get("task") or fallback),
                                "assignee": str(item.get("assignee") or fallback),
                                "deadline": str(item.get("deadline") or fallback)})
        empty_summary = "No summary available" if output_language == "en" else "暂无摘要"
        mindmap = cls._normalize_mindmap(payload.get("mindmap"), output_language)
        timeline = cls._normalize_timeline(payload.get("timeline_chapters"))
        return {"summary": str(payload.get("summary") or empty_summary),
                "decisions": [str(item) for item in decisions], "action_items": actions,
                "mindmap": mindmap, "timeline_chapters": timeline,
                "timeline_anchor_protocol": 1}

    @staticmethod
    def _normalize_timeline(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        chapters = []
        allowed_kinds = {
            "initial", "topic_change", "goal_change", "decision_phase",
            "phase_change", "mark", "unspecified",
        }
        for index, item in enumerate(value[:80]):
            if not isinstance(item, dict):
                continue
            try:
                start_ms = max(0, int(item.get("start_ms") or 0))
                end_ms = max(start_ms, int(item.get("end_ms") or start_ms))
            except (TypeError, ValueError):
                continue
            title = str(item.get("title") or "").strip()[:80]
            points = [str(point).strip()[:180] for point in (item.get("items") or [])[:5]
                      if str(point).strip()]
            if title and points:
                raw_boundary = item.get("boundary")
                if not isinstance(raw_boundary, dict):
                    raw_boundary = {}
                kind = str(raw_boundary.get("kind") or
                           ("initial" if index == 0 else "unspecified")).strip().lower()
                if kind not in allowed_kinds:
                    kind = "unspecified"
                try:
                    confidence = float(raw_boundary.get("confidence") or 0.0)
                except (TypeError, ValueError):
                    confidence = 0.0
                if index == 0 and kind == "initial" and confidence <= 0.0:
                    confidence = 1.0
                confidence = min(1.0, max(0.0, confidence))
                reason = str(raw_boundary.get("reason") or "").strip()[:180]
                anchor = str(item.get("anchor") or "").strip().upper()
                if not re.fullmatch(r"S\d{4,6}", anchor):
                    anchor = ""
                chapters.append({"start_ms": start_ms, "end_ms": end_ms,
                                 "anchor": anchor,
                                 "title": title, "items": points,
                                 "boundary": {"kind": kind,
                                              "confidence": confidence,
                                              "reason": reason}})
        return sorted(chapters, key=lambda item: (item["start_ms"], item["end_ms"]))

    @staticmethod
    def _normalize_mindmap(value: Any, output_language: str = "auto") -> dict[str, Any]:
        fallback_title = "Meeting highlights" if output_language == "en" else "会议重点"
        if not isinstance(value, dict):
            return {"title": fallback_title, "branches": []}
        branches = []
        raw_branches = value.get("branches") if isinstance(value.get("branches"), list) else []
        for branch in raw_branches[:6]:
            if not isinstance(branch, dict):
                continue
            title = str(branch.get("title") or "").strip()
            raw_items = branch.get("items") if isinstance(branch.get("items"), list) else []
            items = [str(item).strip() for item in raw_items[:5] if str(item).strip()]
            if title and items:
                branches.append({"title": title, "items": items})
        return {"title": str(value.get("title") or fallback_title).strip(), "branches": branches}
