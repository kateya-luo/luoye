import React, {useEffect, useMemo, useRef, useState} from 'react';
import {
  createMinutesJob, deleteMeeting, getMeeting, getMeetingAudio, getMinutesJob,
  listMeetingSpeakers, listMeetings, listMinutesTemplates, listPeopleMemory,
  updateMeetingSpeaker, updateMeetingTitle, confirmMeetingMemory,
} from './api';
import CaptionStream from './CaptionStream';
import ProcessingProgress from './ProcessingProgress';
import MindMap, {MindMapPreview} from './MindMap';
import ExportDialog from './ExportDialog';
import {keyConclusions, formatClock, buildChapters} from './summaryDerive';
import {buildSpeakerDirectory} from './speakers';
import {applyMinutesJobResponse} from './minutesJobUi.js';
import {processingStatusLabel} from './processingStatus';
import {
  durationLabel, durationSeconds, participantCount, meetingTags, meetingTitle, shortId, formatBytes,
} from './meetingMeta';
import {
  IconBack, IconExport, IconTrash, IconRefresh, IconHistory, IconChevron, IconClose,
  IconCaptions, IconMinutes, IconMindmap, IconCheckCircle, IconCircle, IconClipboard,
  IconStar, IconMic, IconSignal, IconUser, IconLayout, IconServer, IconFile, IconCheck,
} from './icons';

const fmtDate = (v) => { try { return new Intl.DateTimeFormat('zh-CN', {dateStyle: 'medium', timeStyle: 'short'}).format(new Date(v)); } catch { return v; } };
const fmtClockTime = (v) => { try { return new Date(v).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false}); } catch { return ''; } };
const fmtDay = (v) => { try { const d = new Date(v); return {d: d.getDate(), m: `${d.getMonth() + 1}月`}; } catch { return {d: '--', m: ''}; } };
function dateRangeLabel(meeting) {
  try {
    const start = new Date(meeting.created_at);
    const secs = durationSeconds(meeting);
    const date = `${start.getFullYear()}-${String(start.getMonth() + 1).padStart(2, '0')}-${String(start.getDate()).padStart(2, '0')}`;
    const startT = start.toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
    if (secs == null) return `${date} ${startT}`;
    const end = new Date(start.getTime() + secs * 1000).toLocaleTimeString('zh-CN', {hour: '2-digit', minute: '2-digit', hour12: false});
    return `${date} ${startT} - ${end}（${Math.round(secs / 60)} 分钟）`;
  } catch { return meeting.created_at; }
}

function Tags({tags}) {
  return (
    <div className="row-tags">
      {tags.exported && <span className="tagchip ok"><IconCheck /> 已导出</span>}
      {tags.minutes && <span className="tagchip blue"><IconMinutes /> 有纪要</span>}
      {tags.mindmap && <span className="tagchip purple"><IconMindmap /> 有思维导图</span>}
    </div>
  );
}

/* ---------- list row ---------- */
function MeetingRow({m, active, onOpen, onExport, onDelete, onSelect}) {
  const tags = meetingTags(m, m.session_id);
  const processing = m.processing;
  const statusClass = ['failed', 'stalled'].includes(processing?.stage) ? 'failed' : (processing?.active ? 'processing' : 'ok');
  return (
    <div className={`hrow ${active ? 'active' : ''}`} onClick={() => onSelect(m.session_id)}>
      <div className="hrow-main">
        <div className="hrow-title"><span className="dot" /><strong>{meetingTitle(m)}</strong><span className={`status ${statusClass}`}>{processingStatusLabel(processing)}</span></div>
        <div className="hrow-meta">
          <span><IconHistory /> {fmtDate(m.created_at)}</span>
          <span><IconMic /> 电脑麦克风（模拟录音笔）</span>
          <span>{m.segment_count} 段</span>
        </div>
        <ProcessingProgress status={processing} compact />
        <Tags tags={tags} />
      </div>
      <div className="hrow-actions no-drag" onClick={(e) => e.stopPropagation()}>
        <button className="btn ghost" onClick={() => onOpen(m.session_id)}><IconLayout /> 打开</button>
        <button className="btn ghost" onClick={() => onExport(m.session_id)}><IconExport /> 导出</button>
        <button className="btn ghost danger" onClick={() => onDelete(m.session_id)}><IconTrash /> 删除</button>
      </div>
    </div>
  );
}

/* ---------- right-side preview ---------- */
function PreviewAside({meeting, onOpen, onClose}) {
  if (!meeting) return null;
  const s = meeting.summary || {};
  const concl = keyConclusions(s);
  const tags = meetingTags(meeting, meeting.session_id);
  return (
    <aside className="preview-aside">
      <div className="panel-head">
        <div><h2 style={{fontSize: 16}}>{meetingTitle(meeting)}</h2></div>
        <button className="icon-btn" onClick={onClose}><IconClose /></button>
      </div>
      <div className="panel-body">
        <div className="prev-meta">
          <span><IconHistory /> {dateRangeLabel(meeting)}</span>
          <span><IconMic /> 电脑麦克风（模拟录音笔）</span>
        </div>
        <ProcessingProgress status={meeting.processing} />
        <Tags tags={tags} />
        <div className="aside-sect"><div className="sect-head"><IconClipboard /> 会议摘要</div><p className="muted" style={{lineHeight: 1.7, color: 'var(--ink-2)'}}>{s.summary || '暂无摘要'}</p></div>
        <div className="aside-sect"><div className="sect-head"><IconCheckCircle /> 关键结论</div>
          {concl.length ? concl.slice(0, 4).map((c, i) => <div className="concl" key={i}><IconCheckCircle className="c-done" /><span>{c.text}</span></div>) : <p className="muted">无</p>}
        </div>
        <div className="aside-sect"><div className="sect-head"><IconClipboard /> 待办任务 {s.action_items?.length ? <span className="count">{s.action_items.length}</span> : null}</div>
          {(s.action_items || []).slice(0, 4).map((t, i) => { const ph = (v) => !v || /^(to be confirmed|待确认|tbd|n\/a|—|--)$/i.test(v.trim()); return <div className="todo-item" key={i}><span className="box" /><span className="who">{t.task}</span><span className="meta">{!ph(t.assignee) && <span className="owner">{t.assignee}</span>}{!ph(t.deadline) && <span className="due">{t.deadline}</span>}</span></div>; })}
        </div>
        {s.mindmap?.branches?.length > 0 && <div className="aside-sect"><div className="sect-head"><IconLayout /> 思维导图预览</div><MindMapPreview value={s.mindmap} /></div>}
        <button className="btn primary" style={{width: '100%', marginTop: 14}} onClick={() => onOpen(meeting.session_id)}>打开完整记录</button>
      </div>
    </aside>
  );
}

/* ---------- info column (detail) ---------- */
function InfoColumn({meeting}) {
  const processing = meeting.processing;
  const statusClass = ['failed', 'stalled'].includes(processing?.stage) ? 'failed' : (processing?.active ? 'processing' : 'ok');
  const rows = [
    ['会议名称', meetingTitle(meeting)],
    ['会议状态', <span className={`status ${statusClass}`} key="s">{processingStatusLabel(processing)}</span>],
    ['会议时间', dateRangeLabel(meeting)],
    ['参会人数', `${participantCount(meeting)} 人`],
    ['录音来源', '电脑麦克风（模拟录音笔）'],
    ['网络状态', '正常'],
    ['创建时间', fmtDate(meeting.created_at)],
    ['会议 ID', shortId(meeting.session_id)],
    ['存储位置', '云端存储'],
  ];
  return (
    <section className="panel">
      <div className="panel-head"><h2>会议信息</h2></div>
      <div className="panel-body">
        {rows.map(([k, v], i) => <div className="info-row" key={i}><span className="ik">{k}</span><span className="iv">{v}</span></div>)}
      </div>
    </section>
  );
}

function segmentsToLines(meeting) {
  if (meeting.segments?.length) return meeting.segments.map((s) => ({
    ts: formatClock((s.start_ms || 0) / 1000),
    startMs: s.start_ms ?? null,
    text: s.text,
    translation: s.translation || undefined,   // 双语记录：历史字幕行下显示译文
    speakerId: s.speaker_id,
    speakerLabel: s.speaker_label,
  }));
  return (meeting.transcript || []).map((t) => ({ts: '', startMs: null, text: t}));
}

function fmtAudioTime(secs) {
  const s = Math.floor(secs);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

function AudioPlayer({sessionId, onTimeUpdate}) {
  const audioRef = useRef(null);
  const [playing, setPlaying] = useState(false);
  const [currentTime, setCurrentTime] = useState(0);
  const [duration, setDuration] = useState(0);
  const [error, setError] = useState(false);
  const [src, setSrc] = useState('');

  useEffect(() => {
    let active = true;
    let objectUrl = '';
    setSrc('');
    setError(false);
    getMeetingAudio(sessionId).then((blob) => {
      objectUrl = URL.createObjectURL(blob);
      if (active) setSrc(objectUrl);
      else URL.revokeObjectURL(objectUrl);
    }).catch(() => {
      if (active) setError(true);
    });
    return () => {
      active = false;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [sessionId]);

  const toggle = () => {
    const el = audioRef.current;
    if (!el) return;
    playing ? el.pause() : el.play();
  };

  const seek = (frac) => {
    const el = audioRef.current;
    if (!el || !duration) return;
    el.currentTime = frac * duration;
  };

  useEffect(() => {
    const el = audioRef.current;
    if (!el) return;
    const onPlay = () => setPlaying(true);
    const onPause = () => setPlaying(false);
    const onLoaded = () => setDuration(el.duration || 0);
    const onTime = () => { setCurrentTime(el.currentTime); onTimeUpdate?.(el.currentTime * 1000); };
    const onErr = () => setError(true);
    el.addEventListener('play', onPlay);
    el.addEventListener('pause', onPause);
    el.addEventListener('loadedmetadata', onLoaded);
    el.addEventListener('timeupdate', onTime);
    el.addEventListener('error', onErr);
    return () => {
      el.removeEventListener('play', onPlay);
      el.removeEventListener('pause', onPause);
      el.removeEventListener('loadedmetadata', onLoaded);
      el.removeEventListener('timeupdate', onTime);
      el.removeEventListener('error', onErr);
    };
  }, [src, onTimeUpdate]);

  // Allow parent to call seekToMs
  AudioPlayer._seekRef = (ms) => {
    const el = audioRef.current;
    if (!el) return;
    el.currentTime = ms / 1000;
    el.play();
  };

  if (error) return <div className="audio-player error">录音文件暂不可用</div>;
  if (!src) return <div className="audio-player">正在加载录音…</div>;
  const pct = duration ? (currentTime / duration) * 100 : 0;
  return (
    <div className="audio-player">
      <audio ref={audioRef} src={src} preload="metadata" />
      <button className="ap-btn" onClick={toggle}>{playing ? '⏸' : '▶'}</button>
      <span className="ap-time">{fmtAudioTime(currentTime)}</span>
      <div className="ap-track" onClick={(e) => {
        const rect = e.currentTarget.getBoundingClientRect();
        seek((e.clientX - rect.left) / rect.width);
      }}>
        <div className="ap-fill" style={{width: `${pct}%`}} />
      </div>
      <span className="ap-time">{fmtAudioTime(duration)}</span>
    </div>
  );
}

function MindMapDetailPanel({node, onClose}) {
  if (!node) return null;
  return (
    <div className="mm-detail-panel">
      <div className="mm-dp-head">
        <span style={{fontWeight: 700, color: node.color?.ink || 'var(--brand)'}}>{node.root ? '会议主题' : node.branch ? `${node.branch} › 详情` : '主题分支'}</span>
        <button className="icon-btn" onClick={onClose}><IconClose /></button>
      </div>
      <div className="mm-dp-body">
        <div className="mm-dp-title">{node.title}</div>
        {node.items?.length > 0 && (
          <ul className="mm-dp-items">
            {node.items.map((item, i) => <li key={i}>{item}</li>)}
          </ul>
        )}
      </div>
    </div>
  );
}

function InlineTitle({value, sessionId, onChange}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value || '');
  const inputRef = useRef(null);

  useEffect(() => { if (editing) inputRef.current?.select(); }, [editing]);

  const commit = async () => {
    setEditing(false);
    const trimmed = draft.trim();
    if (!trimmed || trimmed === value) { setDraft(value || ''); return; }
    try {
      await updateMeetingTitle(sessionId, trimmed);
      onChange(trimmed);
    } catch {
      setDraft(value || '');
    }
  };

  if (editing) {
    return (
      <input
        ref={inputRef}
        className="inline-title-input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => { if (e.key === 'Enter') commit(); if (e.key === 'Escape') { setEditing(false); setDraft(value || ''); } }}
        maxLength={200}
      />
    );
  }
  return (
    <h1 onClick={() => { setDraft(value || ''); setEditing(true); }} title="点击编辑会议名称" style={{cursor: 'text'}}>
      {value || '未命名会议'}<span style={{fontSize: 13, marginLeft: 6, opacity: 0.4, fontWeight: 400}}>✎</span>
    </h1>
  );
}

function SpeakerMemoryEditor({meeting, onChanged}) {
  const [speakers, setSpeakers] = useState([]);
  const [people, setPeople] = useState([]);
  const [drafts, setDrafts] = useState({});
  const [message, setMessage] = useState('');
  const load = async () => {
    const [speakerPayload, peoplePayload] = await Promise.all([
      listMeetingSpeakers(meeting.session_id), listPeopleMemory(),
    ]);
    setSpeakers(speakerPayload.speakers || []);
    setPeople(peoplePayload.people || []);
    setDrafts(Object.fromEntries((speakerPayload.speakers || []).map((s) => [s.speaker_id, {
      display_name: s.display_name, role: s.role || '', person_id: s.person_id || '', remember: Boolean(s.remembered),
    }])));
  };
  useEffect(() => { load().catch((e) => setMessage(e.message)); }, [meeting.session_id]);
  const save = async (speakerId) => {
    try {
      const value = drafts[speakerId];
      await updateMeetingSpeaker(meeting.session_id, speakerId, {...value, person_id: value.person_id || null});
      setMessage('人员名称已保存；勾选“记住”后会用于以后会议的声纹匹配和同音字词典。');
      await load();
      onChanged?.();
    } catch (e) { setMessage(e.message); }
  };
  return (
    <section className="panel">
      <div className="panel-head"><h2>参会人员校正</h2><span className="meta">{speakers.length} 人</span></div>
      <div className="panel-body speaker-memory-list">
        {speakers.map((speaker) => {
          const value = drafts[speaker.speaker_id] || {};
          return <div className="speaker-memory-row" key={speaker.speaker_id}>
            <div className="speaker-memory-meta"><b>{speaker.speaker_id}</b><span>{speaker.segment_count} 段 · {Math.round(speaker.duration_ms / 1000)} 秒</span></div>
            <input value={value.display_name || ''} onChange={(e) => setDrafts((d) => ({...d, [speaker.speaker_id]: {...value, display_name: e.target.value}}))} placeholder="姓名" />
            <input value={value.role || ''} onChange={(e) => setDrafts((d) => ({...d, [speaker.speaker_id]: {...value, role: e.target.value}}))} placeholder="角色（可选）" />
            <select value={value.person_id || ''} onChange={(e) => {
              const person = people.find((p) => p.id === e.target.value);
              setDrafts((d) => ({...d, [speaker.speaker_id]: {...value, person_id: e.target.value,
                display_name: person?.display_name || value.display_name, role: person?.role || value.role}}));
            }}>
              <option value="">不关联已有人员</option>
              {people.map((p) => <option key={p.id} value={p.id}>{p.display_name}{p.role ? ` · ${p.role}` : ''}</option>)}
            </select>
            <label className="remember-person"><input type="checkbox" checked={Boolean(value.remember)} onChange={(e) => setDrafts((d) => ({...d, [speaker.speaker_id]: {...value, remember: e.target.checked}}))} /> 记住此人</label>
            <button className="btn primary" onClick={() => save(speaker.speaker_id)}>保存</button>
            {speaker.match_mode === 'suggested' && <span className="memory-suggestion">声纹建议：{speaker.display_name}（{Math.round((speaker.match_confidence || 0) * 100)}%）</span>}
          </div>;
        })}
        {!speakers.length && <p className="muted">本场会议暂未形成可编辑的说话人分组。</p>}
        {message && <p className="muted">{message}</p>}
      </div>
    </section>
  );
}

function TemplateMinutesPanel({meeting, onChanged, onMinutesReady}) {
  const [templates, setTemplates] = useState([]);
  const [selected, setSelected] = useState(meeting.summary?.template?.id || '01');
  const [job, setJob] = useState(null);
  const [error, setError] = useState('');
  const [confirming, setConfirming] = useState(false);
  const [showPicker, setShowPicker] = useState(false);
  useEffect(() => { listMinutesTemplates().then((p) => setTemplates(p.templates || [])).catch((e) => setError(e.message)); }, []);
  useEffect(() => {
    if (meeting.summary?.template?.id) setSelected(meeting.summary.template.id);
  }, [meeting.session_id, meeting.summary?.template?.id]);
  useEffect(() => {
    if (!job || !['queued', 'generating'].includes(job.state)) return undefined;
    const timer = setInterval(async () => {
      try {
        const current = await getMinutesJob(job.id);
        if (['ready', 'failed'].includes(current.state)) clearInterval(timer);
        await applyMinutesJobResponse(current, {setJob, setShowPicker, onMinutesReady, onChanged, setError});
      } catch (e) { setError(e.message); }
    }, 1500);
    return () => clearInterval(timer);
  }, [job?.id, job?.state]);
  const generate = async () => {
    setError('');
    try {
      const current = await createMinutesJob(meeting.session_id, selected);
      await applyMinutesJobResponse(current, {setJob, setShowPicker, onMinutesReady, onChanged, setError});
    }
    catch (e) { setError(e.message); }
  };
  const summary = meeting.summary || {};
  const hasMinutes = meeting.minutes_status === 'ready'
    && Boolean(summary.summary || summary.title || (summary.sections || []).length);
  const grouped = templates.reduce((out, item) => ({...out, [item.category]: [...(out[item.category] || []), item]}), {});
  const confirmCandidates = async () => {
    setConfirming(true);
    try {
      await confirmMeetingMemory(meeting.session_id, (summary.memory_candidates || []).filter((c) => !c.confirmed));
      setError('已把候选内容存入会议记忆。');
    } catch (e) { setError(e.message); }
    finally { setConfirming(false); }
  };
  if (!hasMinutes || showPicker) return (
    <section className="panel template-picker-panel">
      <div className="panel-head"><h2>生成会议纪要</h2><span className="meta">点击后才会调用 DeepSeek 一次</span></div>
      <div className="panel-body">
        <p className="muted">完整转写已经保存。先校正人员姓名，再选择最符合会议目的的模板。</p>
        {Object.entries(grouped).map(([category, items]) => <div className="template-group" key={category}>
          <h3>{category}</h3><div className="template-grid">{items.map((t) => <button key={t.id}
            className={`template-card ${selected === t.id ? 'selected' : ''}`} onClick={() => setSelected(t.id)}>
            <b>{t.id} · {t.name}</b><span>{t.description}</span></button>)}</div>
        </div>)}
        <div className="template-actions"><button className="btn primary" disabled={!templates.length || ['queued', 'generating'].includes(job?.state)} onClick={generate}>
          {['queued', 'generating'].includes(job?.state) ? '正在一次性生成…' : '使用所选模板生成纪要'}
        </button></div>
        {error && <p className="ag-error">{error}</p>}
      </div>
    </section>
  );
  const conclusions = summary.conclusions || {};
  return (
    <section className="panel generated-minutes">
      <div className="panel-head"><div><h2>{summary.title || '会议纪要'}</h2><span className="meta">{summary.template?.name}</span></div>
        <button className="btn ghost" onClick={() => { setJob(null); setShowPicker(true); }}>重新选择模板</button></div>
      <div className="panel-body">
        <div className="minutes-summary"><h3>会议摘要</h3><p>{summary.summary}</p></div>
        {(summary.sections || []).map((section, i) => <div className="minutes-section" key={`${section.heading}-${i}`}><h3>{section.heading}</h3>
          {section.items?.length ? <ul>{section.items.map((item, n) => <li key={n}>{item}</li>)}</ul> : <p className="muted">本场会议未形成相关内容</p>}</div>)}
        {Object.entries(conclusions).some(([, values]) => values?.length) && <div className="minutes-section"><h3>结论分级</h3>
          {Object.entries(conclusions).map(([kind, values]) => values?.length ? <div key={kind}><b>{({decisions:'会议决定',consensus:'已达成共识',tendencies:'倾向意见',suggestions:'个人建议',disagreements:'分歧意见',unresolved:'待确认事项'})[kind] || kind}</b><ul>{values.map((v, i) => <li key={i}>{v}</li>)}</ul></div> : null)}</div>}
        {(summary.action_items || []).length > 0 && <div className="minutes-section"><h3>行动项</h3>{summary.action_items.map((item, i) => <div className="action-card" key={i}><b>{item.task}</b><span>责任人：{item.assignee} · 截止：{item.deadline}</span><span>成果：{item.deliverable} · 关闭标准：{item.closure_standard}</span></div>)}</div>}
        {(summary.memory_candidates || []).length > 0 && <div className="minutes-section memory-candidates"><h3>可沉淀为会议记忆的候选</h3>
          <ul>{summary.memory_candidates.map((item, i) => <li key={i}>{item.content}</li>)}</ul>
          <button className="btn ghost" disabled={confirming} onClick={confirmCandidates}>{confirming ? '保存中…' : '确认写入会议记忆'}</button></div>}
        {error && <p className="muted">{error}</p>}
      </div>
    </section>
  );
}

/* ---------- full detail page ---------- */
function DetailPage({meeting: initialMeeting, onBack, onDelete, onExport}) {
  const [meeting, setMeeting] = useState(initialMeeting);
  const [tab, setTab] = useState('captions');
  const [confirm, setConfirm] = useState(false);
  const [seekMs, setSeekMs] = useState(null);
  const [mmNode, setMmNode] = useState(null);
  const [title, setTitle] = useState(meeting.title || meetingTitle(meeting));
  const audioPlayerRef = useRef(null);

  const s = meeting.summary || {};
  const directory = buildSpeakerDirectory({speakers: s.speakers, speakerRoles: s.speaker_roles});
  const rolesById = new Map([...directory.values()].map((d) => [d.speakerId, d.roleLabel]));
  const lines = segmentsToLines(meeting);
  const chapters = buildChapters(s, new Date(meeting.created_at), durationSeconds(meeting) || 0);
  const concl = keyConclusions(s);
  const summaryPending = ['queued', 'generating'].includes(meeting.minutes_status);
  const transcriptProcessing = Boolean(meeting.processing?.active);
  const refreshMeeting = async () => {
    try {
      setMeeting(await getMeeting(meeting.session_id));
      return true;
    } catch {
      return false;
    }
  };
  const applyMinutesResult = (result) => {
    if (!result) return;
    setMeeting((current) => ({
      ...current,
      summary: result,
      minutes_status: 'ready',
      summary_pending: false,
    }));
  };

  // Auto-refresh while either transcript processing or on-demand minutes generation is active.
  useEffect(() => {
    if (!summaryPending && !transcriptProcessing) return;
    const timer = setInterval(async () => {
      try {
        const fresh = await getMeeting(meeting.session_id);
        setMeeting(fresh);
        if (!fresh.summary_pending && !fresh.processing?.active) clearInterval(timer);
      } catch { /* ignore */ }
    }, 2000);
    return () => clearInterval(timer);
  }, [summaryPending, transcriptProcessing, meeting.session_id]);

  const handleSeek = (ms) => {
    setSeekMs(ms);
    AudioPlayer._seekRef?.(ms);
  };

  const SummarySection = () => summaryPending ? (
    <div className="summary-pending">
      <div className="sp-spin" />
      <span>AI 纪要生成中，字幕记录已可查看…</span>
    </div>
  ) : (
    <>
      <div className="aside-sect"><div className="sect-head"><IconCheckCircle /> 关键结论</div>
        {concl.length ? concl.map((c, i) => <div className="concl" key={i}><IconCheckCircle className="c-done" /><span>{c.text}</span></div>) : <p className="muted">无</p>}</div>
      <div className="aside-sect"><div className="sect-head"><IconClipboard /> 待办事项 {s.action_items?.length ? <span className="count">{s.action_items.length}</span> : null}</div>
        {(s.action_items || []).map((t, i) => {
          const ph = (v) => !v || /^(to be confirmed|待确认|tbd|n\/a|—|--)$/i.test(v.trim());
          return <div className="todo-item" key={i}><span className="box" /><span className="who">{t.task}</span><span className="meta">{!ph(t.assignee) && <span className="owner">{t.assignee}</span>}{!ph(t.deadline) && <span className="due">{t.deadline}</span>}</span></div>;
        })}
        {!s.action_items?.length && <p className="muted">无</p>}</div>
      <div className="aside-sect"><div className="sect-head"><IconStar style={{color: 'var(--brand)'}} /> 决策记录 {s.decisions?.length ? <span className="count">{s.decisions.length}</span> : null}</div>
        {(s.decisions || []).map((d, i) => <div className="decision" key={i}><span className="num">{i + 1}</span><span>{d}</span></div>)}
        {!s.decisions?.length && <p className="muted">无</p>}</div>
    </>
  );

  return (
    <div className="history-detail">
      <div className="detail-top">
        <button className="btn ghost" onClick={onBack}><IconBack /> 返回会议历史</button>
      </div>
      <div className="detail-header">
        <div>
          <div className="dh-title">
            <InlineTitle value={title} sessionId={meeting.session_id} onChange={setTitle} />
            <span className={`status ${['failed', 'stalled'].includes(meeting.processing?.stage) ? 'failed' : (meeting.processing?.active ? 'processing' : 'ok')}`}>{processingStatusLabel(meeting.processing)}</span>
          </div>
          <div className="dh-meta">
            <span><IconHistory /> {dateRangeLabel(meeting)}</span>
            <span><IconUser /> {participantCount(meeting)} 人参会</span>
            <span><IconMic /> 录音来源：电脑麦克风（模拟录音笔）</span>
            <span className="net-ok"><IconSignal /> 网络：正常</span>
          </div>
        </div>
        <div className="dh-actions no-drag">
          <button className="btn ghost" onClick={() => onExport(meeting)}><IconExport /> 导出</button>
          <div style={{position: 'relative'}}>
            <button className="btn ghost danger" onClick={() => setConfirm((v) => !v)}><IconTrash /> 删除会议</button>
            {confirm && (
              <div className="confirm-pop" onClick={(e) => e.stopPropagation()}>
                <div className="cp-head"><IconClose /> 确认删除这场会议吗？</div>
                <p>删除后不可恢复。</p>
                <div className="cp-actions">
                  <button className="btn ghost" onClick={() => setConfirm(false)}>取消</button>
                  <button className="btn primary" style={{background: 'var(--red)'}} onClick={onDelete}>确认删除</button>
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <ProcessingProgress status={meeting.processing} />

      {meeting.has_audio && (
        <div style={{marginBottom: 12}} ref={audioPlayerRef}>
          <AudioPlayer sessionId={meeting.session_id} onTimeUpdate={setSeekMs} />
        </div>
      )}

      {(meeting.marks || []).length > 0 && (
        <section className="panel" style={{marginBottom: 12}}>
          <div className="panel-head"><h2>重点标记</h2><span className="meta">{meeting.marks.length} 处</span></div>
          <div className="panel-body" style={{display: 'flex', gap: 8, flexWrap: 'wrap'}}>
            {meeting.marks.map((mark) => <button key={mark.id} className="btn ghost"
              onClick={() => handleSeek(mark.at_ms)}><IconStar />
              {formatClock(mark.at_ms / 1000)}{mark.label ? ` · ${mark.label}` : ' · 重点'}</button>)}
          </div>
        </section>
      )}

      <div className="subtabs detail-subtabs" style={{borderBottom: '1px solid var(--border)', marginBottom: 16}}>
        {[{id: 'captions', label: '完整转写与人员', icon: <IconCaptions />}, {id: 'minutes', label: '会议纪要', icon: <IconMinutes />}].map((t) => (
          <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => setTab(t.id)}>{t.icon}{t.label}</button>
        ))}
      </div>

      {tab === 'captions' && (
        <div className="detail-transcript-layout">
          <section className="panel"><div className="panel-head"><h2>字幕记录</h2><span className="meta">{lines.length} 段{meeting.has_audio ? ' · 点击跳转录音' : ''}</span></div>
            <div className="panel-body" style={{maxHeight: 520}}>
              <CaptionStream lines={lines} partial="" recording={false} rolesById={rolesById} autoScroll={false}
                onSeek={meeting.has_audio ? handleSeek : undefined} activeSeekMs={seekMs} />
            </div>
          </section>
          <div className="detail-transcript-side"><SpeakerMemoryEditor meeting={meeting} onChanged={refreshMeeting} /><InfoColumn meeting={meeting} /></div>
        </div>
      )}

      {tab === 'minutes' && (
        <TemplateMinutesPanel meeting={meeting} onChanged={refreshMeeting} onMinutesReady={applyMinutesResult} />
      )}
    </div>
  );
}

/* ---------- main ---------- */
export default function HistoryPanel({refreshKey, onUnauthorized}) {
  const [meetings, setMeetings] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [query, setQuery] = useState('');
  const [dateRange, setDateRange] = useState('all');
  const [sort, setSort] = useState('new');
  const [previewId, setPreviewId] = useState(null);
  const [previewMeeting, setPreviewMeeting] = useState(null);
  const [openMeeting, setOpenMeeting] = useState(null);
  const [exportMeetingData, setExportMeetingData] = useState(null);

  const handleError = (e) => { if (e.status === 401) onUnauthorized(); else setError(e.message); };
  const refresh = async () => {
    setLoading(true); setError('');
    try { setMeetings((await listMeetings()).meetings); } catch (e) { handleError(e); } finally { setLoading(false); }
  };
  useEffect(() => { refresh(); }, [refreshKey]);

  const hasBackgroundWork = meetings.some((meeting) => meeting.processing?.active);
  useEffect(() => {
    if (!hasBackgroundWork) return undefined;
    const timer = setInterval(async () => {
      try {
        const next = (await listMeetings()).meetings;
        setMeetings(next);
        if (previewId) {
          const preview = await getMeeting(previewId);
          setPreviewMeeting(preview);
        }
      } catch { /* keep the last visible progress during a transient network error */ }
    }, 2500);
    return () => clearInterval(timer);
  }, [hasBackgroundWork, previewId]);

  const fetchMeeting = async (sid) => { try { return await getMeeting(sid); } catch (e) { handleError(e); return null; } };
  const select = async (sid) => { setPreviewId(sid); setPreviewMeeting(null); setPreviewMeeting(await fetchMeeting(sid)); };
  const open = async (sid) => { const m = await fetchMeeting(sid); if (m) setOpenMeeting(m); };
  const startExport = async (sidOrMeeting) => {
    const m = typeof sidOrMeeting === 'string' ? await fetchMeeting(sidOrMeeting) : sidOrMeeting;
    if (m) setExportMeetingData(m);
  };
  const remove = async (sid) => {
    try { await deleteMeeting(sid); setOpenMeeting(null); setPreviewId(null); setPreviewMeeting(null); await refresh(); }
    catch (e) { handleError(e); }
  };

  const filtered = useMemo(() => {
    const now = Date.now();
    const within = {all: Infinity, '7d': 7 * 864e5, '30d': 30 * 864e5}[dateRange] ?? Infinity;
    let list = meetings.filter((m) => {
      const text = `${m.summary || ''} ${m.transcript_preview || ''}`.toLowerCase();
      if (query && !text.includes(query.toLowerCase())) return false;
      if (within !== Infinity && now - new Date(m.created_at).getTime() > within) return false;
      return true;
    });
    list = [...list].sort((a, b) => sort === 'old'
      ? new Date(a.created_at) - new Date(b.created_at)
      : new Date(b.created_at) - new Date(a.created_at));
    return list;
  }, [meetings, query, dateRange, sort]);

  // Estimate local text cache from real per-meeting content (≈240 B/segment + summary text).
  const usedBytes = useMemo(() => meetings.reduce((sum, m) => sum + (m.segment_count || 0) * 240 + ((m.summary || '').length + (m.transcript_preview || '').length) * 3, 0), [meetings]);

  if (openMeeting) {
    return (
      <>
        <DetailPage meeting={openMeeting} onBack={() => setOpenMeeting(null)}
          onDelete={() => remove(openMeeting.session_id)} onExport={startExport} />
        {exportMeetingData && <ExportDialog meeting={exportMeetingData} onClose={() => setExportMeetingData(null)} onExported={refresh} />}
      </>
    );
  }

  return (
    <div className="history-layout">
      <div className={`history-main ${previewMeeting || previewId ? 'with-preview' : ''}`}>
        <div className="history-toolbar" style={{flexWrap: 'nowrap', gap: 8}}>
          <div className="search-box" style={{flex: 1, minWidth: 0}}><IconHistory /><input placeholder="搜索会议标题 / 关键词" value={query} onChange={(e) => setQuery(e.target.value)} /></div>
          <div className="filter-chip" style={{flexShrink: 0}}><select value={dateRange} onChange={(e) => setDateRange(e.target.value)}><option value="all">日期：全部</option><option value="7d">近 7 天</option><option value="30d">近 30 天</option></select><IconChevron /></div>
        </div>

        <div className="history-subbar">
          <span>共 {filtered.length} 个会议记录</span>
          <div className="filter-chip" style={{marginLeft: 'auto'}}><select value={sort} onChange={(e) => setSort(e.target.value)}><option value="new">按日期（最新）</option><option value="old">按日期（最早）</option></select><IconChevron /></div>
        </div>

        {error && <div className="form-error">{error}</div>}
        {loading ? <div className="empty">正在加载会议记录…</div>
          : filtered.length ? (
            <div className="hrow-list">
              {filtered.map((m) => (
                <MeetingRow key={m.session_id} m={m} active={previewId === m.session_id}
                  onSelect={select} onOpen={open} onExport={startExport} onDelete={remove} />
              ))}
            </div>
          ) : <div className="empty"><div className="e-ic"><IconHistory /></div><div>没有匹配的会议记录</div></div>}

        <div className="footerbar" style={{marginTop: 12, borderTop: '1px solid var(--border)', borderRadius: 0}}>
          <div className="fb-item"><IconServer /> 本地存储空间</div>
          <div className="storage-bar"><i style={{width: `${Math.min(100, Math.max(3, usedBytes / (50 * 1024 * 1024) * 100))}%`}} /></div>
          <div className="fb-item">约 {formatBytes(usedBytes)} 文本缓存 · 共 {meetings.length} 场</div>
          <div className="spacer" />
          <div className="fb-item fb-hide-mobile">最小化后继续录音</div>
        </div>
      </div>

      {(previewMeeting || previewId) && (
        <PreviewAside meeting={previewMeeting} onOpen={open} onClose={() => { setPreviewId(null); setPreviewMeeting(null); }} />
      )}

      {exportMeetingData && <ExportDialog meeting={exportMeetingData} onClose={() => setExportMeetingData(null)} onExported={refresh} />}
    </div>
  );
}
