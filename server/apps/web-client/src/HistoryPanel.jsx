import React, {useEffect, useMemo, useRef, useState} from 'react';
import {deleteMeeting, getMeeting, getMeetingAudio, listMeetings, updateMeetingTitle} from './api';
import CaptionStream from './CaptionStream';
import MindMap, {MindMapPreview} from './MindMap';
import {RollingMinutes} from './MeetingPanels';
import ExportDialog from './ExportDialog';
import {keyConclusions, formatClock, buildChapters} from './summaryDerive';
import {buildSpeakerDirectory} from './speakers';
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
  return (
    <div className={`hrow ${active ? 'active' : ''}`} onClick={() => onSelect(m.session_id)}>
      <div className="hrow-main">
        <div className="hrow-title"><span className="dot" /><strong>{meetingTitle(m)}</strong><span className="status ok">已完成</span></div>
        <div className="hrow-meta">
          <span><IconHistory /> {fmtDate(m.created_at)}</span>
          <span><IconMic /> 电脑麦克风（模拟录音笔）</span>
          <span>{m.segment_count} 段</span>
        </div>
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
  const rows = [
    ['会议名称', meetingTitle(meeting)],
    ['会议状态', <span className="status ok" key="s">已完成</span>],
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
  const summaryPending = meeting.summary_pending;

  // Auto-refresh when summary is still generating
  useEffect(() => {
    if (!summaryPending) return;
    const timer = setInterval(async () => {
      try {
        const fresh = await getMeeting(meeting.session_id);
        if (!fresh.summary_pending) { setMeeting(fresh); clearInterval(timer); }
      } catch { /* ignore */ }
    }, 8000);
    return () => clearInterval(timer);
  }, [summaryPending, meeting.session_id]);

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
            <span className="status ok">已完成</span>
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
        {[{id: 'captions', label: '字幕记录', icon: <IconCaptions />}, {id: 'minutes', label: '滚动纪要', icon: <IconMinutes />}, {id: 'mindmap', label: '思维导图', icon: <IconMindmap />}].map((t) => (
          <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => setTab(t.id)}>{t.icon}{t.label}</button>
        ))}
      </div>

      {tab === 'captions' && (
        <div className="cols c-3b" style={{height: 'auto'}}>
          <section className="panel"><div className="panel-head"><h2>字幕记录</h2><span className="meta">{lines.length} 段{meeting.has_audio ? ' · 点击跳转录音' : ''}</span></div>
            <div className="panel-body" style={{maxHeight: 520}}>
              <CaptionStream lines={lines} partial="" recording={false} rolesById={rolesById} autoScroll={false}
                onSeek={meeting.has_audio ? handleSeek : undefined} activeSeekMs={seekMs} />
            </div>
          </section>
          <section className="panel"><div className="panel-head"><h2>会议摘要</h2></div>
            <div className="panel-body"><SummarySection /></div>
          </section>
          <InfoColumn meeting={meeting} />
        </div>
      )}

      {tab === 'minutes' && (
        <div className="cols c-2" style={{height: 'auto'}}>
          <section className="panel"><div className="panel-head"><h2>滚动纪要</h2></div>
            <div className="panel-body">
              {summaryPending ? <div className="summary-pending"><div className="sp-spin" /><span>AI 纪要生成中…</span></div> : <RollingMinutes chapters={chapters} summary={s.summary} onSeek={meeting.has_audio ? handleSeek : undefined} />}
            </div>
          </section>
          <InfoColumn meeting={meeting} />
        </div>
      )}

      {tab === 'mindmap' && (
        <div style={{display: 'flex', gap: 12, height: 560}}>
          <section className="panel" style={{flex: 1, overflow: 'hidden'}}>
            <div className="panel-head"><h2>思维导图</h2>{summaryPending && <span className="meta">生成中…</span>}</div>
            <div style={{flex: 1, minHeight: 0}}>
              {summaryPending
                ? <div className="summary-pending"><div className="sp-spin" /><span>AI 纪要生成中…</span></div>
                : <MindMap value={s.mindmap} onSelect={setMmNode} selectedKey={mmNode?.key} />}
            </div>
          </section>
          {mmNode && <MindMapDetailPanel node={mmNode} onClose={() => setMmNode(null)} />}
        </div>
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
