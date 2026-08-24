// Client-side meeting exporter. Honors section checkboxes + info page + watermark,
// so partial exports work without backend changes. TXT / Markdown / Word(.doc HTML) / JSON.
import {durationLabel, durationSeconds, participantCount, shortId, meetingTitle, markExported} from './meetingMeta';
import {buildChapters} from './summaryDerive';
import {roleLabel} from './speakers';

function lines(meeting) {
  if (meeting.segments?.length) return meeting.segments.map((s) =>
    `[${s.speaker_label || '说话人'}] ${s.text}` + (s.translation ? `\n    ↳ ${s.translation}` : ''));
  return meeting.transcript || [];
}

function infoBlock(meeting, account) {
  const s = meeting.summary || {};
  return [
    ['会议名称', meetingTitle(meeting)],
    ['会议状态', '已完成'],
    ['会议时间', meeting.created_at],
    ['时长', durationLabel(meeting)],
    ['参会人数', `${participantCount(meeting)} 人`],
    ['录音来源', '电脑麦克风（模拟录音笔）'],
    ['创建者', account || '本地账户'],
    ['会议 ID', shortId(meeting.session_id)],
    ['存储位置', '云端存储'],
  ];
}

function buildSections(meeting, opts) {
  const s = meeting.summary || {};
  const out = [];
  if (opts.includeInfo) out.push({h: '会议信息', kind: 'kv', rows: infoBlock(meeting, opts.account)});
  if (opts.sections.minutes) {
    out.push({h: '摘要', kind: 'p', text: s.summary || '暂无摘要'});
    if (s.speaker_roles?.length) out.push({h: '说话人角色', kind: 'list', items: s.speaker_roles.map((r) => `${r.speaker_id}：${roleLabel(r.role)}${r.description ? `（${r.description}）` : ''}`)});
    out.push({h: '关键结论 / 决策', kind: 'list', items: (s.decisions || []).length ? s.decisions : ['（无）']});
    out.push({h: '待办事项', kind: 'list', items: (s.action_items || []).length ? s.action_items.map((a) => `${a.task}（负责人：${a.assignee}；截止：${a.deadline}）`) : ['（无）']});
  }
  if (opts.sections.mindmap && s.mindmap?.branches?.length) {
    out.push({h: '思维导图', kind: 'list', items: s.mindmap.branches.map((b) => `${b.title}：${(b.items || []).join('；')}`)});
  }
  if (opts.sections.captions) out.push({h: '完整字幕记录', kind: 'list', items: lines(meeting)});
  return out;
}

function toText(meeting, opts) {
  const parts = [`会议纪要 · ${meetingTitle(meeting)}`, ''];
  for (const sec of buildSections(meeting, opts)) {
    parts.push(sec.h);
    if (sec.kind === 'p') parts.push(sec.text);
    else if (sec.kind === 'kv') sec.rows.forEach(([k, v]) => parts.push(`${k}：${v}`));
    else sec.items.forEach((i) => parts.push(`- ${i}`));
    parts.push('');
  }
  if (opts.watermark) parts.push(`—— 由 Clear Meeting 于 ${new Date().toLocaleString('zh-CN', {hour12: false})} 导出`);
  return parts.join('\n');
}

function toMarkdown(meeting, opts) {
  const parts = [`# 会议纪要 · ${meetingTitle(meeting)}`, ''];
  for (const sec of buildSections(meeting, opts)) {
    parts.push(`## ${sec.h}`, '');
    if (sec.kind === 'p') parts.push(sec.text, '');
    else if (sec.kind === 'kv') { sec.rows.forEach(([k, v]) => parts.push(`- **${k}**：${v}`)); parts.push(''); }
    else { sec.items.forEach((i) => parts.push(`- ${i}`)); parts.push(''); }
  }
  if (opts.watermark) parts.push('---', `*由 Clear Meeting 于 ${new Date().toLocaleString('zh-CN', {hour12: false})} 导出*`);
  return parts.join('\n');
}

function clockOf(ms) {
  const s = Math.floor((ms || 0) / 1000);
  const hh = Math.floor(s / 3600), mm = Math.floor(s / 60) % 60, ss = s % 60;
  const p = (n) => String(n).padStart(2, '0');
  return hh > 0 ? `${hh}:${p(mm)}:${p(ss)}` : `${p(mm)}:${p(ss)}`;
}

// Word(.doc) 导出：MSO 兼容 HTML，设计感排版——彩色封面 / 信息表 / 摘要 / 结论 / 待办表 /
// 滚动纪要时间线(取代思维导图) / 双语字幕表。用背景色带 + 细横线做层次，避开"表格网格"的廉价感。
function toWordHtml(meeting, opts) {
  const esc = (v) => String(v ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  // 克制的双色：靛蓝主色 + 深墨字 + 淡背景带，只有强调处见色
  const AC = '#4338ca', AC2 = '#eef1fb', INK = '#111827', SUB = '#6b7280', HAIR = '#e6e8ef', BAND = '#f7f8fb';
  const s = meeting.summary || {};
  const body = [];
  // 章节标题：小号靛蓝眉标 + 大标题 + 一条细底线（不用重边框，留白见档次）
  const H = (eyebrow, title) => `
    <p style="margin:26pt 0 0;font-size:9pt;color:${AC};letter-spacing:2pt;font-weight:bold">${esc(eyebrow)}</p>
    <p style="margin:1pt 0 4pt;font-size:14pt;color:${INK};font-weight:bold">${esc(title)}</p>
    <div style="border-bottom:0.75pt solid ${HAIR};margin-bottom:10pt"></div>`;

  // —— 封面：整幅靛蓝色带 ——
  body.push(`<table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse;margin-bottom:6pt">
    <tr><td style="background:${AC};padding:22pt 24pt">
      <p style="margin:0;font-size:9pt;color:#c7d0f7;letter-spacing:3pt">CLEAR MEETING · 会议纪要</p>
      <p style="margin:6pt 0 0;font-size:23pt;color:#ffffff;font-weight:bold;line-height:1.2">${esc(meetingTitle(meeting))}</p>
      <p style="margin:8pt 0 0;font-size:10pt;color:#d5dcf8">${esc(meeting.created_at || '')} 　·　 ${esc(durationLabel(meeting))} 　·　 ${participantCount(meeting)} 人参会</p>
    </td></tr></table>`);

  // —— 会议信息：无竖线、行底细横线，label 淡色 ——
  if (opts.includeInfo) {
    const rows = infoBlock(meeting, opts.account);
    body.push(H('OVERVIEW', '会议信息'));
    body.push(`<table cellspacing="0" cellpadding="0" style="border-collapse:collapse;width:100%">${
      rows.map(([k, v]) => `<tr>
        <td style="padding:5pt 0;width:96pt;color:${SUB};font-size:10pt;border-bottom:0.5pt solid ${HAIR};vertical-align:top">${esc(k)}</td>
        <td style="padding:5pt 0 5pt 12pt;color:${INK};font-size:10.5pt;border-bottom:0.5pt solid ${HAIR}">${esc(v)}</td></tr>`).join('')
    }</table>`);
  }

  if (opts.sections.minutes) {
    // —— 摘要：淡背景带引文块 ——
    body.push(H('SUMMARY', '会议摘要'));
    body.push(`<table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse">
      <tr><td style="background:${BAND};border-left:3pt solid ${AC};padding:12pt 16pt;font-size:11pt;color:${INK};line-height:1.9;text-align:justify">${esc(s.summary || '暂无摘要')}</td></tr></table>`);

    // —— 关键结论：靛蓝序号 ——
    body.push(H('DECISIONS', '关键结论 / 决策'));
    const decisions = (s.decisions || []);
    body.push(decisions.length
      ? `<table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse">${decisions.map((d, i) => `<tr>
          <td style="width:22pt;padding:4pt 0;color:${AC};font-weight:bold;font-size:11pt;vertical-align:top">${String(i + 1).padStart(2, '0')}</td>
          <td style="padding:4pt 0;font-size:10.5pt;color:${INK};line-height:1.8">${esc(d)}</td></tr>`).join('')}</table>`
      : `<p style="font-size:10pt;color:${SUB};margin:0">（本场会议未识别到明确决策）</p>`);

    // —— 待办：只留横线的三列表 + 彩色表头 ——
    body.push(H('ACTION ITEMS', '待办事项'));
    const todos = (s.action_items || []);
    body.push(todos.length
      ? `<table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse">
          <tr>${[['任务', ''], ['负责人', '78pt'], ['截止', '86pt']].map(([th, w]) =>
            `<td style="background:${AC};color:#fff;padding:5pt 10pt;font-size:10pt;font-weight:bold;text-align:left;${w ? `width:${w}` : ''}">${th}</td>`).join('')}</tr>${
          todos.map((a, i) => `<tr>
            <td style="padding:6pt 10pt;font-size:10.5pt;color:${INK};background:${i % 2 ? BAND : '#fff'};border-bottom:0.5pt solid ${HAIR}">${esc(a.task)}</td>
            <td style="padding:6pt 10pt;font-size:10.5pt;color:${INK};background:${i % 2 ? BAND : '#fff'};border-bottom:0.5pt solid ${HAIR}">${esc(a.assignee)}</td>
            <td style="padding:6pt 10pt;font-size:10.5pt;color:${INK};background:${i % 2 ? BAND : '#fff'};border-bottom:0.5pt solid ${HAIR}">${esc(a.deadline)}</td></tr>`).join('')
        }</table>`
      : `<p style="font-size:10pt;color:${SUB};margin:0">（本场会议未识别到待办事项）</p>`);
  }

  // —— 滚动纪要时间线（取代思维导图；由 buildChapters 从纪要要点分段生成，便于快速检索）——
  const chapters = buildChapters(s, meeting.created_at ? new Date(meeting.created_at) : null, durationSeconds(meeting) || 0);
  if (opts.sections.minutes && chapters.length) {
    body.push(H('ROLLING MINUTES', `滚动纪要 · ${chapters.length} 个要点`));
    body.push(`<table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse">${
      chapters.map((c) => `<tr>
        <td style="width:52pt;padding:8pt 8pt 8pt 0;vertical-align:top">
          <table cellspacing="0" cellpadding="0"><tr><td style="background:${AC2};color:${AC};font-size:9pt;font-weight:bold;padding:2pt 6pt;text-align:center">${esc(c.time)}</td></tr></table>
        </td>
        <td style="padding:8pt 0 8pt 12pt;border-left:1.5pt solid ${HAIR};vertical-align:top">
          <p style="margin:0 0 4pt;font-size:11.5pt;font-weight:bold;color:${INK}">${esc(c.title)}</p>
          ${(c.items || []).map((it) => `<p style="margin:0 0 3pt;font-size:10.5pt;color:${INK};line-height:1.7">· ${esc(it)}</p>`).join('')}
        </td></tr>`).join('')
    }</table>`);
  }

  // —— 完整字幕记录（双语：时间 | 说话人 | 内容+译文）——
  if (opts.sections.captions) {
    const segs = meeting.segments || [];
    body.push(H('TRANSCRIPT', `完整字幕记录 · ${(segs.length || (meeting.transcript || []).length)} 段`));
    if (segs.length) {
      body.push(`<table cellspacing="0" cellpadding="0" style="width:100%;border-collapse:collapse">
        <tr>${[['时间', '44pt'], ['说话人', '60pt'], ['内容', '']].map(([th, w]) =>
          `<td style="background:${AC};color:#fff;padding:4pt 10pt;font-size:9.5pt;font-weight:bold;text-align:left;${w ? `width:${w}` : ''}">${th}</td>`).join('')}</tr>${
        segs.map((seg, i) => `<tr>
          <td style="padding:5pt 10pt;font-size:9pt;color:${SUB};background:${i % 2 ? BAND : '#fff'};border-bottom:0.5pt solid ${HAIR};vertical-align:top">${clockOf(seg.start_ms)}</td>
          <td style="padding:5pt 10pt;font-size:9pt;color:${AC};font-weight:bold;background:${i % 2 ? BAND : '#fff'};border-bottom:0.5pt solid ${HAIR};vertical-align:top">${esc(seg.speaker_label || '说话人')}</td>
          <td style="padding:5pt 10pt;font-size:10.5pt;color:${INK};background:${i % 2 ? BAND : '#fff'};border-bottom:0.5pt solid ${HAIR};line-height:1.65">${esc(seg.text)}${
            seg.translation ? `<br><span style="color:${SUB};font-size:9.5pt">译　${esc(seg.translation)}</span>` : ''}</td></tr>`).join('')
      }</table>`);
    } else {
      body.push(`<p style="font-size:10.5pt;color:${INK};line-height:1.9">${(meeting.transcript || []).map(esc).join('<br>')}</p>`);
    }
  }

  if (opts.watermark) body.push(`<p style="color:#9ca3af;font-size:9pt;margin-top:22pt;border-top:0.5pt solid ${HAIR};padding-top:8pt;text-align:center"><i>由 Clear Meeting 于 ${esc(new Date().toLocaleString('zh-CN', {hour12: false}))} 导出</i></p>`);
  return `<!doctype html><html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word">
<head><meta charset="utf-8"><!--[if gte mso 9]><xml><w:WordDocument><w:View>Print</w:View><w:Zoom>100</w:Zoom></w:WordDocument></xml><![endif]-->
<style>@page{margin:2.2cm}body{font-family:"Microsoft YaHei","PingFang SC",sans-serif;color:${INK}}</style></head>
<body>${body.join('')}</body></html>`;
}

function toJson(meeting, opts) {
  const s = meeting.summary || {};
  const payload = {session_id: meeting.session_id, created_at: meeting.created_at, title: meetingTitle(meeting)};
  if (opts.includeInfo) payload.info = Object.fromEntries(infoBlock(meeting, opts.account));
  if (opts.sections.minutes) payload.summary = {summary: s.summary, decisions: s.decisions, action_items: s.action_items, speaker_roles: s.speaker_roles};
  if (opts.sections.mindmap) payload.mindmap = s.mindmap;
  if (opts.sections.captions) payload.segments = meeting.segments?.length ? meeting.segments : (meeting.transcript || []);
  if (opts.watermark) payload.exported_at = new Date().toISOString();
  return JSON.stringify(payload, null, 2);
}

const FORMATS = {
  word: {ext: 'doc', mime: 'application/msword', build: toWordHtml},
  markdown: {ext: 'md', mime: 'text/markdown;charset=utf-8', build: toMarkdown},
  txt: {ext: 'txt', mime: 'text/plain;charset=utf-8', build: toText},
  json: {ext: 'json', mime: 'application/json;charset=utf-8', build: toJson},
};

export const SUPPORTED_FORMATS = Object.keys(FORMATS);

function download(filename, mime, content) {
  const blob = new Blob([content], {type: mime});
  if (window.ClearMeetingAndroid?.saveFile) {
    const reader = new FileReader();
    reader.onload = () => window.ClearMeetingAndroid.saveFile(filename, mime, String(reader.result).split(',', 2)[1]);
    reader.readAsDataURL(blob);
    return;
  }
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); a.remove();
  URL.revokeObjectURL(url);
}

export function composeExport(meeting, format, options) {
  const spec = FORMATS[format];
  if (!spec) throw new Error(`暂不支持的格式：${format}`);
  const content = spec.build(meeting, options);
  download(`${shortId(meeting.session_id) || 'meeting'}.${spec.ext}`, spec.mime, content);
  markExported(meeting.session_id);
  return {bytes: new TextEncoder().encode(content).length};
}
