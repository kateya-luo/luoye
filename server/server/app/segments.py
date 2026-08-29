"""时间锚定的字幕分段与时间轴（设计文档 §4 / §7 / §8）。

Timeline 是本架构的几何核心：分段按会议时间偏移 start_ms 有序排列，
实时(provisional)分段可被离线(final)分段按时间区间**原位替换**，
补洞与会议定稿都归结为对某个 [start_ms, end_ms] 区间执行 apply_offline()。

这一层不依赖 FastAPI / FunASR，纯数据逻辑，便于单测。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field, asdict
from typing import Any, Iterable, Literal

SegState = Literal["provisional", "filling", "final"]
SegSource = Literal["live", "offline", "offline_canonical", "gap"]


def _new_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Segment:
    start_ms: int
    end_ms: int
    text: str = ""
    speaker_id: str | None = None
    speaker_label: str | None = None   # 显示用标签（如"说话人 1"）；离线段暂为 None
    speaker_final: bool = False
    source: SegSource = "live"
    state: SegState = "provisional"
    seg_id: str = field(default_factory=_new_id)
    revision: int = 1

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Patch:
    """一次时间轴变更，用于通过 WS 广播给客户端（消息类型 segments_patch）。"""
    added: list[Segment] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)   # seg_id 列表

    def to_dict(self) -> dict[str, Any]:
        return {
            "patches": [s.to_dict() for s in self.added],
            "removed": list(self.removed),
        }


class Timeline:
    """按 start_ms 有序维护分段集，支持实时 upsert、缺口登记、离线区间替换。"""

    def __init__(self) -> None:
        self._by_id: dict[str, Segment] = {}

    # ---- 读 ----
    def ordered(self) -> list[Segment]:
        return sorted(self._by_id.values(), key=lambda s: (s.start_ms, s.end_ms))

    def to_list(self) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self.ordered()]

    def to_transcript_lines(self) -> list[str]:
        """按时间序导出带说话人标签的转写行（含离线补洞段），供落盘与纪要生成。"""
        return [
            f"[{s.speaker_label}] {s.text}" if s.speaker_label else s.text
            for s in self.ordered() if s.text
        ]

    # ---- 实时（通道 A）----
    def upsert_live(self, seg: Segment) -> Patch:
        """新增或更新一个实时分段（provisional）。已存在则按 seg_id 覆盖并自增 revision。"""
        existing = self._by_id.get(seg.seg_id)
        if existing:
            seg.revision = existing.revision + 1
        self._by_id[seg.seg_id] = seg
        return Patch(added=[seg])

    # ---- 缺口（断网区间占位）----
    def mark_gap(self, start_ms: int, end_ms: int) -> Patch:
        """登记一段"补传转写中"的占位，前端据此显示 ⏳ 占位卡。"""
        seg = Segment(start_ms=start_ms, end_ms=end_ms, text="", source="gap", state="filling")
        self._by_id[seg.seg_id] = seg
        return Patch(added=[seg])

    # ---- 离线（通道 B 转写完成，权威）----
    def apply_offline(self, start_ms: int, end_ms: int, segments: Iterable[Segment]) -> Patch:
        """用离线权威分段替换 [start_ms, end_ms) 内的所有现有分段（按 start_ms 归属判定）。

        gap 补洞：range 为断网区间；finalize 整场重转：range 为 [0, 会议时长]。
        返回 Patch（新增的 final 分段 + 被移除的旧分段 id），供广播与持久化。
        """
        removed = [sid for sid, s in self._by_id.items() if start_ms <= s.start_ms < end_ms]
        for sid in removed:
            del self._by_id[sid]
        added: list[Segment] = []
        for s in segments:
            s.source = "offline"
            s.state = "final"
            if not s.seg_id:
                s.seg_id = _new_id()
            self._by_id[s.seg_id] = s
            added.append(s)
        return Patch(added=added, removed=removed)

    # ---- 离线补洞（additive：只填空洞，保留已有实时段含说话人）----
    def fill_gaps(self, offline_segments: Iterable[Segment]) -> Patch:
        """把离线分段中**与现有任何分段都不在时间上重叠**的，插入时间轴。

        断网区间本就没有实时段 → 离线段无重叠 → 被插入；连接正常区间实时段已占位
        （带说话人）→ 离线段重叠 → 跳过，避免覆盖实时结果/丢说话人。
        """
        existing = list(self._by_id.values())
        added: list[Segment] = []
        for o in offline_segments:
            if any(o.start_ms < e.end_ms and e.start_ms < o.end_ms for e in existing):
                continue  # 与已有段重叠，跳过
            o.source = "offline"
            o.state = "final"
            if not o.seg_id:
                o.seg_id = _new_id()
            self._by_id[o.seg_id] = o
            added.append(o)
            existing.append(o)  # 让后续离线段也不要彼此重叠
        return Patch(added=added)

    # ---- 说话人全局对齐（会议结束，§9）----
    def relabel_speakers(self, mapping: dict[str, str]) -> Patch:
        """把局部 speaker_id 按全局映射重写，并标记 speaker_final=True。"""
        changed: list[Segment] = []
        for s in self._by_id.values():
            new_id = mapping.get(s.speaker_id) if s.speaker_id else None
            if new_id is not None and (new_id != s.speaker_id or not s.speaker_final):
                s.speaker_id = new_id
                s.speaker_final = True
                s.revision += 1
                changed.append(s)
        return Patch(added=changed)

    # ---- 恢复（从持久化加载）----
    @classmethod
    def from_list(cls, items: Iterable[dict[str, Any]]) -> "Timeline":
        tl = cls()
        for it in items:
            seg = Segment(**{k: it[k] for k in it if k in Segment.__dataclass_fields__})
            tl._by_id[seg.seg_id] = seg
        return tl
