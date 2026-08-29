// Derive display metadata for archived meetings from real backend data.
import {formatClock} from './summaryDerive.js';

const EXPORTED_KEY = 'clear-meeting-exported';
const encoder = new TextEncoder();

export const byteLen = (value) => encoder.encode(typeof value === 'string' ? value : JSON.stringify(value || '')).length;

export function formatBytes(n) {
  if (!n) return '0 KB';
  if (n < 1024 * 1024) return `${Math.max(1, Math.round(n / 1024))} KB`;
  return `${(n / 1024 / 1024).toFixed(1)} MB`;
}

// Duration in seconds from segment timing, or null when unavailable.
export function durationSeconds(meeting) {
  const segs = meeting?.segments || [];
  const end = segs.reduce((max, s) => Math.max(max, s.end_ms || 0), 0);
  return end ? Math.round(end / 1000) : null;
}
export const durationLabel = (meeting) => { const s = durationSeconds(meeting); return s == null ? '—' : formatClock(s); };

export function participantCount(meeting) {
  const summary = meeting?.summary || meeting || {};
  if (summary.speakers?.length) return summary.speakers.length;
  const ids = new Set((meeting?.segments || []).map((s) => s.speaker_id).filter(Boolean));
  if (ids.size) return ids.size;
  return meeting?.segments?.length || meeting?.transcript?.length ? 1 : 0;
}

export function meetingTags(meeting, sessionId) {
  // List items expose `summary` as a string (the title); detail exposes it as an object.
  const summary = meeting?.summary;
  const isDetail = summary && typeof summary === 'object';
  return {
    // Every saved meeting carries minutes (the backend always stores a summary on meeting_end).
    minutes: true,
    // Mind map presence is only known once the detail (with branches) is loaded.
    mindmap: isDetail ? Boolean(summary.mindmap?.branches?.length) : false,
    exported: isExported(sessionId),
  };
}

export function sectionSizes(meeting) {
  const summary = meeting?.summary || {};
  const captions = byteLen((meeting?.segments?.length ? meeting.segments.map((s) => `${s.speaker_label || ''} ${s.text}`) : meeting?.transcript || []).join('\n'));
  const minutes = byteLen(summary.summary) + byteLen(summary.decisions) + byteLen(summary.action_items);
  const mindmap = byteLen(summary.mindmap);
  return {captions, minutes, mindmap};
}

export const shortId = (sessionId) => {
  if (!sessionId) return '';
  const hex = String(sessionId).replace(/[^0-9a-z]/gi, '');
  return `cm_${hex.slice(0, 8)}`;
};

export function meetingTitle(meeting) {
  if (meeting?.title) return meeting.title;
  const summary = meeting?.summary;
  // 详情对象：summary 是 object，有 mindmap / decisions 等字段
  if (summary && typeof summary === 'object') {
    if (typeof summary.title === 'string' && summary.title.trim()) return summary.title.trim();
    if (summary.mindmap?.branches?.length && summary.mindmap.title) return summary.mindmap.title;
    if (typeof summary.summary === 'string' && summary.summary.length) return summary.summary.slice(0, 28);
  }
  // 列表项：summary 是 AI 生成的摘要字符串
  if (typeof summary === 'string' && summary.length && summary !== '暂无摘要') return summary.slice(0, 28);
  return (meeting?.transcript_preview || '').replace(/\[说话人\s*\d+\]\s*/g, '').trim().slice(0, 28) || '未命名会议';
}

export function safeFileBaseName(value, fallback = '未命名会议') {
  let name = String(value || '').normalize('NFKC')
    .replace(/[\u0000-\u001f<>:"/\\|?*]/g, '-')
    .replace(/\s+/g, ' ').trim()
    .replace(/[. ]+$/g, '');
  if (!name) name = fallback;
  if (/^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i.test(name)) name = `${name}-会议`;
  return Array.from(name).slice(0, 80).join('');
}

export const meetingExportBaseName = (meeting) => safeFileBaseName(meetingTitle(meeting));

// "已导出" tracking (client-side, since backend doesn't record it).
export function isExported(sessionId) {
  try { return JSON.parse(localStorage.getItem(EXPORTED_KEY) || '[]').includes(sessionId); }
  catch { return false; }
}
export function markExported(sessionId) {
  try {
    const set = new Set(JSON.parse(localStorage.getItem(EXPORTED_KEY) || '[]'));
    set.add(sessionId);
    localStorage.setItem(EXPORTED_KEY, JSON.stringify([...set]));
  } catch { /* ignore */ }
}
