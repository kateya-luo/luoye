import React, {useEffect, useRef, useState, Component} from 'react';
import CaptionStream from './CaptionStream';
import ProcessingProgress from './ProcessingProgress';
import {getMeetingProcessing} from './api';

class ErrorBoundary extends Component {
  state = {error: null};
  static getDerivedStateFromError(e) { return {error: e}; }
  render() {
    if (this.state.error) {
      return (
        <div style={{padding: 20, color: 'red', background: '#fff', fontSize: 13, wordBreak: 'break-all'}}>
          <b>渲染错误（请截图发给开发者）：</b>
          <pre style={{whiteSpace: 'pre-wrap', marginTop: 8}}>{this.state.error?.message}{'\n'}{this.state.error?.stack}</pre>
        </div>
      );
    }
    return this.props.children;
  }
}
import {MeetingStatusBar, OfflineBanner, ReconnectAside, FooterBar} from './MeetingChrome';
import {MeetingAssistAside} from './MeetingPanels';
import {buildChapters, countWords, chapterCount, formatClock} from './summaryDerive';
import {buildSpeakerDirectory, roleLabel} from './speakers';
import {listMicrophones, startMicrophoneCapture} from './microphoneCapture';
import {createAudioUploader} from './audioUploader';
import {IconPlay, IconPause, IconStop, IconBookmark, IconSignal, IconWifiOff} from './icons';

function useIsMobile() {
  const [mobile, setMobile] = useState(() => window.matchMedia('(max-width: 480px)').matches);
  useEffect(() => {
    const mq = window.matchMedia('(max-width: 480px)');
    const handler = (e) => setMobile(e.matches);
    mq.addEventListener('change', handler);
    return () => mq.removeEventListener('change', handler);
  }, []);
  return mobile;
}

const MOBILE_SUBTABS = [
  {id: 'control', label: '会议控制'},
  {id: 'captions', label: '实时字幕'},
];

const desktopServerUrl = window.clearMeetingDesktop?.serverUrl || '';
const desktopWsBase = desktopServerUrl ? `${desktopServerUrl.replace(/^http/, 'ws').replace(/\/$/, '')}/ws` : '';
const defaultWsBase = import.meta.env.VITE_WS_URL || desktopWsBase || `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`;
const emptyResult = {summary: '', decisions: [], action_items: [], mindmap: {title: '', branches: []}, speakers: [], speaker_roles: []};
const createSessionId = () => globalThis.crypto?.randomUUID?.() || `session-${Date.now()}-${Math.random().toString(16).slice(2)}`;

function normalizeWsBase(value) {
  let input = value.trim();
  if (!input) return defaultWsBase;
  if (!/^[a-z]+:\/\//i.test(input)) input = `${location.protocol === 'https:' ? 'wss' : 'ws'}://${input}`;
  const url = new URL(input);
  if (location.protocol === 'https:') url.protocol = 'wss:';
  else if (url.protocol === 'http:') url.protocol = 'ws:';
  if (!['ws:', 'wss:'].includes(url.protocol)) throw new Error('地址必须使用 ws:// 或 wss://');
  url.pathname = `${url.pathname.replace(/\/$/, '') || ''}/ws`.replace('/ws/ws', '/ws');
  return url.toString().replace(/\/$/, '');
}

function MobileControlTab({recording, paused, online, elapsedSec, microphones, microphoneId, onMicChange, result, processing, speakerEnabled, onSpeakerToggle, translateTo, onTranslateChange, onStart, onStop, onPause, onMark}) {
  const points = [
    ...(result?.decisions || []).map((text) => ({text, done: true})),
    ...(result?.action_items || []).map((t) => ({text: t.task, done: false})),
  ].slice(0, 5);
  return (
    <div className="mobile-ctrl-tab">
      {/* 状态卡 */}
      <div className="mct-status-card">
        <div className={`rec-badge ${recording && !paused ? 'live' : ''}`} style={{fontSize: 14}}>
          <span className="pulse" />{recording ? (paused ? '已暂停' : '录音中') : '待开始'}
        </div>
        <div className="mct-time">{formatClock(elapsedSec)}</div>
        <div className={`mct-net ${online ? 'ok' : 'bad'}`}>
          {online ? <IconSignal /> : <IconWifiOff />}{online ? '网络正常' : '网络异常'}
        </div>
      </div>
      <ProcessingProgress status={processing} />

      {/* 麦克风选择 */}
      <div className="mct-mic">
        <select value={microphoneId} onChange={(e) => onMicChange(e.target.value)} disabled={recording}>
          <option value="">电脑麦克风（模拟录音笔）</option>
          {microphones.map((d, i) => <option key={d.deviceId} value={d.deviceId}>{d.label || `麦克风 ${i + 1}`}</option>)}
        </select>
      </div>

      {/* 说话人识别开关 */}
      <label className="mct-toggle" style={{opacity: recording ? 0.5 : 1}}>
        <span className="mct-toggle-label">
          <span>说话人识别</span>
          <span className="mct-toggle-sub">开启后区分不同说话人</span>
        </span>
        <div className={`mct-switch ${speakerEnabled ? 'on' : ''}`} onClick={() => !recording && onSpeakerToggle(!speakerEnabled)}>
          <div className="mct-switch-thumb" />
        </div>
      </label>

      {/* 实时翻译（开始前选择；录制中锁定） */}
      <label className="mct-toggle" style={{opacity: recording ? 0.5 : 1}}>
        <span className="mct-toggle-label">
          <span>实时翻译</span>
          <span className="mct-toggle-sub">每句字幕下显示译文</span>
        </span>
        <select value={translateTo || ''} disabled={recording} onChange={(e) => onTranslateChange?.(e.target.value)}
          style={{height: 34, borderRadius: 10, border: '1px solid var(--border, #d5dbe8)', padding: '0 10px', fontSize: 14, background: 'var(--panel, #fff)', color: 'var(--ink, #1d2740)'}}>
          <option value="">关闭</option>
          <option value="zh">译为中文</option>
          <option value="en">译为英文</option>
          <option value="ja">译为日文</option>
          <option value="ko">译为韩文</option>
        </select>
      </label>

      {/* 控制按钮 */}
      <div className="mct-btns">
        <button className="mct-btn primary" onClick={onStart} disabled={recording}>
          <IconPlay /><span>开始会议</span>
        </button>
        <button className="mct-btn warn" onClick={onPause} disabled={!recording}>
          <IconPause /><span>{paused ? '继续录音' : '暂停录音'}</span>
        </button>
        <button className="mct-btn danger" onClick={onStop} disabled={!recording}>
          <IconStop /><span>结束会议</span>
        </button>
        <button className="mct-btn" onClick={onMark} disabled={!recording}>
          <IconBookmark /><span>标记重点</span>
        </button>
      </div>

      {/* 要点预览 */}
      {points.length > 0 && (
        <div className="mct-points">
          <div className="mct-points-head">当前要点</div>
          {points.map((p, i) => (
            <div className="mct-point" key={i}>
              <span className={`mct-dot ${p.done ? 'done' : ''}`} />
              <span>{p.text}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

export default function MeetingView({token, onMeetingSaved, onCloud, controlMode = false, onRecordingChange}) {
  const isMobile = useIsMobile();
  const [mobileTab, setMobileTab] = useState('captions');
  const [recording, setRecording] = useState(false);
  const [paused, setPaused] = useState(false);
  const [status, setStatus] = useState('待开始');
  const [lines, setLines] = useState([]);
  const [partial, setPartial] = useState('');
  const [partialSpeaker, setPartialSpeaker] = useState(null);
  const [result, setResult] = useState(emptyResult);
  const [isFinal, setIsFinal] = useState(false);
  const [updatedAt, setUpdatedAt] = useState('');
  const [sessionId, setSessionId] = useState(createSessionId());
  const [language] = useState('auto');
  const [speakerEnabled, setSpeakerEnabled] = useState(true);
  const [translateTo, setTranslateTo] = useState('');   // 空=不翻译；en/zh/ja/ko
  const translateRef = useRef('');
  const [microphones, setMicrophones] = useState([]);
  const [microphoneId, setMicrophoneId] = useState('');
  const [level, setLevel] = useState(0);
  const [elapsedSec, setElapsedSec] = useState(0);
  const [online, setOnline] = useState(true);
  const [offlineSec, setOfflineSec] = useState(0);
  const [attempt, setAttempt] = useState(0);
  const [markers, setMarkers] = useState([]);
  const [savedAt, setSavedAt] = useState('');
  const [processing, setProcessing] = useState(null);
  const [awaitingProcessing, setAwaitingProcessing] = useState(false);

  const ws = useRef(null);
  const pipeline = useRef(null);
  const pausedRef = useRef(false);
  const uploader = useRef(null);       // 通道B：可靠音频上传器
  const intentionalClose = useRef(false);
  const reconnectTimer = useRef(null);
  const startedAt = useRef(null);

  const directory = buildSpeakerDirectory({speakers: result.speakers, speakerRoles: result.speaker_roles});
  const rolesById = new Map([...directory.values()].map((s) => [s.speakerId, s.roleLabel]));
  const stats = {chapters: chapterCount(result), words: countWords(lines), duration: formatClock(elapsedSec)};
  const chapters = buildChapters(result, startedAt.current, elapsedSec);
  const meetingTitle = result.mindmap?.branches?.length ? result.mindmap.title : '新的会议';

  // 桌面悬浮窗：把录音状态上报给 Electron 主进程（浏览器里 reportMeetingState 不存在，静默跳过）
  useEffect(() => {
    window.clearMeetingDesktop?.reportMeetingState?.({
      title: meetingTitle, micOn: recording, recording, paused, elapsedLabel: formatClock(elapsedSec),
    });
  }, [recording, paused, elapsedSec, meetingTitle]);
  useEffect(() => () => window.clearMeetingDesktop?.reportMeetingState?.({recording: false}), []);

  // elapsed timer
  useEffect(() => {
    if (!recording) return undefined;
    const t = window.setInterval(() => {
      if (!pausedRef.current && startedAt.current) setElapsedSec(Math.floor((Date.now() - startedAt.current) / 1000));
    }, 1000);
    return () => window.clearInterval(t);
  }, [recording]);

  // offline timer
  useEffect(() => {
    if (online) { setOfflineSec(0); return undefined; }
    const t = window.setInterval(() => setOfflineSec((s) => s + 1), 1000);
    return () => window.clearInterval(t);
  }, [online]);

  const applyResult = (message, final) => {
    setResult(message.result || {summary: message.summary || '暂无摘要', decisions: message.decisions || [], action_items: message.action_items || [], mindmap: message.mindmap || {title: '会议重点', branches: []}, speakers: [], speaker_roles: []});
    setIsFinal(final);
    setUpdatedAt(new Date().toLocaleTimeString('zh-CN', {hour12: false}));
    setSavedAt(new Date().toLocaleTimeString('zh-CN', {hour12: false}));
    setStatus(final ? '完整转写已保存，可在历史记录中选择模板生成纪要' : '字幕已更新');
    if (final) onMeetingSaved?.();
  };

  const handleMessage = (event) => {
    const message = JSON.parse(event.data);
    if (message.type === 'asr_result') {
      if (message.is_final) {
        setLines((cur) => {
          const index = message.seg_id ? cur.findIndex((line) => line.seg_id === message.seg_id) : -1;
          const line = {seg_id: message.seg_id, startMs: message.start_ms, endMs: message.end_ms,
            text: message.text, ts: new Date().toLocaleTimeString('zh-CN', {hour12: false}),
            speakerId: message.speaker_id, speakerLabel: message.speaker_label};
          if (index >= 0) return cur.map((item, i) => i === index ? {...item, ...line} : item);
          return cur.at(-1)?.text === message.text ? cur : [...cur, line];
        });
        setPartial(''); setPartialSpeaker(null);
      } else {
        setPartial((cur) => `${cur}${message.text}`);
        setPartialSpeaker(message.speaker_id || null);
      }
    }
    if (message.type === 'translation') {
      // 实时翻译：按 seg_id 把译文挂到对应字幕行下
      setLines((cur) => cur.map((l) => l.seg_id === message.seg_id ? {...l, translation: message.text} : l));
    }
    if (message.type === 'session_resumed') {
      // 重连后服务端补发已累积状态，仅在本地为空时回填，避免与已有字幕重复
      setLines((cur) => cur.length ? cur : (message.segments || []).map((s) => ({
        seg_id: s.seg_id, startMs: s.start_ms, endMs: s.end_ms,
        text: s.text, ts: '', speakerId: s.speaker_id, speakerLabel: s.speaker_label,
      })));
      if (message.summary) setResult(message.summary);
    }
    if (message.type === 'segments_patch') {
      // 离线补洞：按 seg_id 原位插入/替换，并清理被覆盖的占位
      setLines((cur) => {
        const removed = new Set(message.removed || []);
        const added = message.patches || [];
        let next = cur.filter((l) => !removed.has(l.seg_id));
        next = next.filter((l) => !(l.state === 'filling' && added.some((p) => l.startMs < p.end_ms && p.start_ms < l.endMs)));
        for (const p of added) {
          const line = {seg_id: p.seg_id, startMs: p.start_ms, endMs: p.end_ms, text: p.text, ts: '', speakerId: p.speaker_id, speakerLabel: p.speaker_label, state: p.state};
          const idx = next.findIndex((l) => l.seg_id === p.seg_id);
          if (idx >= 0) next[idx] = line; else next.push(line);
        }
        return next;
      });
    }
    if (message.type === 'gap_marker') {
      const gid = `gap-${message.start_ms}`;
      setLines((cur) => cur.some((l) => l.seg_id === gid) ? cur
        : [...cur, {seg_id: gid, startMs: message.start_ms, endMs: message.end_ms, text: '', state: 'filling'}]);
    }
    if (message.type === 'meeting_update') {
      // V0.21 录制期间不再生成滚动纪要。
    }
    if (message.type === 'meeting_result') {
      const ready = message.result?.state === 'transcript_ready' && !message.pending;
      setIsFinal(ready);
      setStatus(ready ? '完整转写已保存，可在历史记录中选择模板生成纪要' : '录音已保存，正在整理完整转写…');
      setAwaitingProcessing(!ready);
      if (ready) { setProcessing({active: false, stage: 'completed', progress_percent: 100}); onMeetingSaved?.(); }
    }
    if (message.type === 'error') setStatus(`服务端错误：${message.message}`);
  };

  const connect = (nextSessionId) => new Promise((resolve, reject) => {
    let base;
    try { base = normalizeWsBase(defaultWsBase); } catch (e) { reject(e); return; }
    const query = token ? `?token=${encodeURIComponent(token)}` : '';
    const socket = new WebSocket(`${base}/${nextSessionId}${query}`);
    socket.binaryType = 'arraybuffer';
    ws.current = socket;
    socket.onopen = () => {
      // 带上真实音频偏移：重连时服务端据此把时间轴推进到真实时间，让断网段成为干净的洞（交由离线补洞填）
      socket.send(JSON.stringify({type: 'start_session', language, enable_speaker: speakerEnabled,
        translate_to: translateRef.current,
        offset_ms: uploader.current?.offsetMs ?? 0}));
      setOnline(true); setAttempt(0); onCloud?.('connected');
      resolve(socket);
    };
    socket.onerror = () => reject(new Error(`无法连接 ${base}`));
    socket.onmessage = handleMessage;
    socket.onclose = (e) => {
      if (intentionalClose.current) return;
      if (recording || pipeline.current) { setOnline(false); onCloud?.('offline'); scheduleReconnect(nextSessionId); }
      else setStatus(e.code === 1008 ? '登录已失效' : '已断开');
    };
  });

  const scheduleReconnect = (nextSessionId) => {
    if (reconnectTimer.current) return;
    reconnectTimer.current = window.setTimeout(async () => {
      reconnectTimer.current = null;
      setAttempt((a) => a + 1);
      try { await connect(nextSessionId); } catch { scheduleReconnect(nextSessionId); }
    }, 4000);
  };

  // 实时流纯尽力而为：连着就发，断了就丢（断网音频由通道B可靠上传 + 离线补洞填回，
  // 不再向实时ASR补传残缺音频——那会在断网区间产生残缺字幕、挡住离线补洞）。
  const sendFrame = (buffer) => {
    const socket = ws.current;
    if (socket && socket.readyState === WebSocket.OPEN) socket.send(buffer);
  };

  const start = async () => {
    try {
      const nextSessionId = createSessionId();
      setSessionId(nextSessionId);
      setLines([]); setPartial(''); setPartialSpeaker(null); setResult(emptyResult);
      setIsFinal(false); setUpdatedAt(''); setMarkers([]); setElapsedSec(0);
      setProcessing(null); setAwaitingProcessing(false);
      intentionalClose.current = false; pausedRef.current = false; setPaused(false);
      startedAt.current = Date.now();
      ws.current && (intentionalClose.current = true, ws.current.close());
      intentionalClose.current = false;
      // 通道B：可靠音频上传器（与实时WS并行，失败重传保证后端拿到完整音频）
      uploader.current?.stop();
      uploader.current = createAudioUploader({sessionId: nextSessionId, token, serverBase: desktopServerUrl});
      const socket = await connect(nextSessionId);
      pipeline.current = await startMicrophoneCapture({
        deviceId: microphoneId,
        onFrame: (buffer) => { if (!pausedRef.current) { sendFrame(buffer); uploader.current?.push(buffer); } },
        onStats: (s) => setLevel(s.level),
      });
      setMicrophones(await listMicrophones());
      setRecording(true);
      setStatus('录音中');
      onCloud?.('connected');
      acquireWakeLock();   // 手机浏览器：锁屏/息屏会中断麦克风采集，录音期间保持屏幕常亮
    } catch (e) { setStatus(`启动失败：${e.message}`); }
  };

  // —— 屏幕常亮锁（移动端录音保命）：录音中持有；切回前台时自动重新获取；不支持的浏览器静默跳过 ——
  const wakeLock = useRef(null);
  const acquireWakeLock = async () => {
    try { wakeLock.current = await navigator.wakeLock?.request?.('screen'); } catch {}
  };
  const releaseWakeLock = () => { try { wakeLock.current?.release(); } catch {} wakeLock.current = null; };
  useEffect(() => {
    const onVis = () => { if (document.visibilityState === 'visible' && pipeline.current) acquireWakeLock(); };
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, []);

  const stop = async () => {
    const p = pipeline.current;
    if (!p) return;
    await p.stop({flush: true});
    pipeline.current = null;
    releaseWakeLock();
    setRecording(false); setPaused(false); pausedRef.current = false;
    uploader.current?.finish();   // 通道B：标记最后一片 final；积压在后台继续传，不阻塞结束
    // 立刻发 meeting_end（趁 WS 还活着）——让服务器明确知道是"主动结束"，不会误挂起等 2 小时重连。
    // 断网补传的音频在后台继续上传，服务器 defer_finalize 会等 final 音频到齐后再出最终纪要，一个字不丢。
    setStatus('正在保存录音并整理完整转写…');
    setAwaitingProcessing(true);
    try { ws.current?.send(JSON.stringify({type: 'meeting_end'})); } catch {}
    // HTTP 兜底（幂等）：WS 已死/僵尸时结束信号也能送达，彻底杜绝"挂起等 2 小时才出纪要"。
    // WS 看似正常时延迟 4 秒再发（服务端若已在处理会直接忽略）；WS 明显已断则立即发。
    const endUrl = `${desktopServerUrl}/api/v1/sessions/${sessionId}/end`;
    const endHeaders = token ? {Authorization: `Bearer ${token}`} : {};
    const fireHttpEnd = () => { fetch(endUrl, {method: 'POST', headers: endHeaders, keepalive: true}).catch(() => {}); };
    if (ws.current?.readyState === WebSocket.OPEN) window.setTimeout(fireHttpEnd, 4000);
    else fireHttpEnd();
  };

  useEffect(() => {
    if (!awaitingProcessing || recording || !sessionId) return undefined;
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await getMeetingProcessing(sessionId, token);
        if (cancelled) return;
        setProcessing(next);
        setStatus(next.title || '后台处理中');
        if (next.stage === 'completed') {
          setAwaitingProcessing(false);
          setIsFinal(true);
          onMeetingSaved?.();
        } else if (['failed', 'stalled'].includes(next.stage)) {
          setAwaitingProcessing(false);
          setIsFinal(false);
        }
      } catch { /* meeting creation and the end signal may cross by a few milliseconds */ }
    };
    poll();
    const timer = window.setInterval(poll, 2000);
    return () => { cancelled = true; window.clearInterval(timer); };
  }, [awaitingProcessing, recording, sessionId, token]);

  const togglePause = () => { pausedRef.current = !pausedRef.current; setPaused(pausedRef.current); setStatus(pausedRef.current ? '已暂停' : '录音中'); };
  const mark = () => { const last = lines.at(-1); setMarkers((m) => [...m, {ts: Date.now(), text: last?.text || ''}]); };

  useEffect(() => {
    const refresh = () => listMicrophones().then(setMicrophones).catch(() => setMicrophones([]));
    refresh();
    navigator.mediaDevices?.addEventListener?.('devicechange', refresh);
    return () => {
      navigator.mediaDevices?.removeEventListener?.('devicechange', refresh);
      intentionalClose.current = true;
      if (reconnectTimer.current) window.clearTimeout(reconnectTimer.current);
      pipeline.current?.stop({flush: false});
      uploader.current?.stop();
      ws.current?.close();
    };
  }, []);

  useEffect(() => { onRecordingChange?.(recording); }, [recording]);

  const controls = {recording, paused, onStart: start, onStop: stop, onPause: togglePause, onMark: mark};
  const queued = {captions: uploader.current?.backlog ?? 0, updates: 0};

  const CaptionsPanel = ({title = '实时字幕', meta}) => (
    <section className="panel">
      <div className="panel-head"><h2>{title}</h2>{meta ?? (recording && <span className="live-dot">实时识别中</span>)}</div>
      <div className="panel-body">
        <CaptionStream lines={lines} partial={partial} partialSpeaker={partialSpeaker} recording={recording && online} rolesById={rolesById} />
      </div>
    </section>
  );

  // ── 手机端 ──
  if (isMobile) {
    // 控制tab（由底部导航控制，MeetingView 收到 controlMode=true）
    if (controlMode) {
      return (
        <ErrorBoundary>
          <div style={{height: '100%', overflow: 'auto'}}>
            <MobileControlTab
              recording={recording} paused={paused} online={online} elapsedSec={elapsedSec}
              microphones={microphones} microphoneId={microphoneId} onMicChange={setMicrophoneId}
              result={result} processing={processing} speakerEnabled={speakerEnabled} onSpeakerToggle={setSpeakerEnabled}
              translateTo={translateTo} onTranslateChange={(v) => { setTranslateTo(v); translateRef.current = v; }}
              {...controls}
            />
          </div>
        </ErrorBoundary>
      );
    }

    // 录音过程中只显示实时转写，纪要在会后按模板生成。
    const CONTENT_TABS = [
      {id: 'captions', label: '实时字幕'},
    ];
    return (
      <ErrorBoundary>
      <div style={{height: '100%', display: 'flex', flexDirection: 'column'}}>
        <MeetingStatusBar recording={recording} paused={paused} elapsedSec={elapsedSec}
          title={meetingTitle} microphones={microphones} microphoneId={microphoneId}
          onMicChange={setMicrophoneId} online={online} level={level} />
        <ProcessingProgress status={processing} />

        <div className="subtabs mobile-subtabs">
          {CONTENT_TABS.map((t) => (
            <button key={t.id} className={mobileTab === t.id ? 'active' : ''} onClick={() => setMobileTab(t.id)}>
              {t.label}
            </button>
          ))}
        </div>

        {!online && <OfflineBanner offlineSec={offlineSec} attempt={attempt} queued={queued} />}

        <div className="scroll-area" style={{flex: 1, minHeight: 0}}>
          {mobileTab === 'captions' && (
            <section className="panel">
              <div className="panel-head">
                <h2>实时字幕</h2>
                {recording && online && <span className="live-dot">实时识别中</span>}
              </div>
              <div className="panel-body">
                <CaptionStream lines={lines} partial={partial} partialSpeaker={partialSpeaker} recording={recording && online} rolesById={rolesById} />
              </div>
            </section>
          )}
        </div>
      </div>
      </ErrorBoundary>
    );
  }

  // ── 桌面端：录制期只保留字幕与控制，不生成纪要。──
  return (
    <div className="app-root" style={{height: '100%'}}>
      <MeetingStatusBar recording={recording} paused={paused} elapsedSec={elapsedSec}
        title={meetingTitle} microphones={microphones} microphoneId={microphoneId}
        onMicChange={setMicrophoneId} online={online} level={level} />
      <ProcessingProgress status={processing} />

      {!online && <OfflineBanner offlineSec={offlineSec} attempt={attempt} queued={queued} />}

      <div className="scroll-area">
        <div className="cols c-2">
          <CaptionsPanel />
          <section className="panel">
            {online
              ? <MeetingAssistAside result={result} controls={controls} stats={stats} online={online} savedAt={savedAt} speakerEnabled={speakerEnabled} onSpeakerToggle={setSpeakerEnabled}
                  translateTo={translateTo} onTranslateChange={(v) => { setTranslateTo(v); translateRef.current = v; }} />
              : <ReconnectAside offlineSec={offlineSec} attempt={attempt} nextRetrySec={null} queued={queued} savedAt={savedAt} />}
          </section>
        </div>
        <div style={{height: 18}} />
      </div>

      <FooterBar recording={recording} online={online} savedAt={savedAt} markers={markers.length} />
    </div>
  );
}
