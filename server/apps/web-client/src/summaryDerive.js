// Pure helpers that map the backend result + caption lines onto the rich UI widgets.
// Everything here is derived from real data — no invented content.

export function countWords(lines) {
  return lines.reduce((sum, line) => sum + (line.text ? line.text.replace(/\s/g, '').length : 0), 0);
}

export function formatClock(totalSeconds) {
  const s = Math.max(0, Math.floor(totalSeconds));
  const hh = String(Math.floor(s / 3600)).padStart(2, '0');
  const mm = String(Math.floor((s % 3600) / 60)).padStart(2, '0');
  const ss = String(s % 60).padStart(2, '0');
  return `${hh}:${mm}:${ss}`;
}

// Rolling-minutes timeline cards, built from the mind map branches (theme + items).
// startedAt: Date when recording began; elapsedSec: seconds since then.
export function buildChapters(result, startedAt, elapsedSec) {
  const timeline = Array.isArray(result?.timeline_chapters) ? result.timeline_chapters : [];
  if (timeline.length) {
    const base = startedAt ? +new Date(startedAt) : Date.now() - (elapsedSec || 0) * 1000;
    return timeline.map((chapter, index) => ({
      chapterNo: chapter.chapter_no || index + 1,
      startMs: Number(chapter.start_ms) || 0,
      endMs: Number(chapter.end_ms) || 0,
      time: new Date(base + (Number(chapter.start_ms) || 0)).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false}),
      title: chapter.title,
      items: chapter.items || [],
      marks: chapter.marks || [],
      markCount: Number(chapter.mark_count) || 0,
      badge: chapter.status === 'current' ? 'add' : 'ok',
      badgeText: chapter.status === 'current' ? '生成中' : '已冻结',
    }));
  }
  const branches = result?.mindmap?.branches || [];
  if (!branches.length) return [];
  const base = startedAt ? +startedAt : Date.now();  // +startedAt handles both Date object and number
  const total = branches.length;
  return branches.map((branch, i) => {
    const offset = total > 1 ? (i / total) * elapsedSec * 1000 : 0;
    const when = new Date(base + offset);
    return {
      time: when.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false}),
      title: branch.title,
      items: branch.items || [],
      badge: i === total - 1 ? 'add' : 'ok',
      badgeText: i === total - 1 ? '新增' : '已确认',
    };
  });
}

// "关键结论" — prefer explicit decisions, fall back to the lead item of each branch.
export function keyConclusions(result) {
  if (result?.decisions?.length) return result.decisions.map((text) => ({text, kind: 'done'}));
  const branches = result?.mindmap?.branches || [];
  return branches
    .filter((b) => (b.items || []).length)
    .map((b) => ({text: b.items[0], kind: 'wait'}));
}

export const chapterCount = (result) => (result?.mindmap?.branches?.length || 0);
