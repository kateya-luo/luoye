import React, {useEffect, useRef, useState} from 'react';
import CaptionStream from './CaptionStream';
import {formatClock} from './summaryDerive';
import {buildSpeakerDirectory} from './speakers';
import {IconCaptions, IconBack, IconSignal} from './icons';

const SUBTABS = [
  {id: 'captions', label: '字幕', icon: <IconCaptions />},
];

const emptyResult = {
  summary: '等待字幕中…',
  decisions: [],
  action_items: [],
  mindmap: {title: '会议重点', branches: []},
  speakers: [],
  speaker_roles: [],
};

function buildWsUrl(sessionId, token) {
  const desktopBase = window.clearMeetingDesktop?.serverUrl || '';
  let base = desktopBase
    ? desktopBase.replace(/^http/, 'ws').replace(/\/$/, '')
    : `${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}`;
  return `${base}/ws/observe/${sessionId}${token ? `?token=${encodeURIComponent(token)}` : ''}`;
}

export default function ObserverView({sessionId, sessionInfo, token, onBack, embedded = false,
  onEnded, onUnavailable}) {
  const [lines, setLines] = useState([]);
  const [partial, setPartial] = useState('');
  const [partialSpeaker, setPartialSpeaker] = useState(null);
  const [result, setResult] = useState(emptyResult);
  const [connected, setConnected] = useState(false);
  const [subtab, setSubtab] = useState('captions');
  const [elapsedSec, setElapsedSec] = useState(0);
  const [updatedAt, setUpdatedAt] = useState('');

  const ws = useRef(null);
  const reconnectTimer = useRef(null);
  const dead = useRef(false);

  const startedAt = sessionInfo?.started_at ? new Date(sessionInfo.started_at) : null;

  // Elapsed timer based on server-reported start time
  useEffect(() => {
    if (!startedAt) return;
    const tick = () => setElapsedSec(Math.floor((Date.now() - startedAt.getTime()) / 1000));
    tick();
    const t = setInterval(tick, 1000);
    return () => clearInterval(t);
  }, []);

  const applyResult = (msg, isSummary) => {
    const r = msg.result || {
      summary: msg.summary || emptyResult.summary,
      decisions: msg.decisions || [],
      action_items: msg.action_items || [],
      mindmap: msg.mindmap || emptyResult.mindmap,
      speakers: msg.speakers || [],
      speaker_roles: msg.speaker_roles || [],
    };
    setResult(r);
    setUpdatedAt(new Date().toLocaleTimeString('zh-CN', {hour12: false}));
  };

  const handleMessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === 'observer_catchup') {
      const segs = msg.segments || [];
      setLines(segs.map((s) => ({
        text: s.text,
        ts: formatClock((s.start_ms || 0) / 1000),
        startMs: s.start_ms ?? null,
        speakerId: s.speaker_id,
        speakerLabel: s.speaker_label,
      })));
      if (msg.summary) applyResult({result: msg.summary}, false);
    }
    if (msg.type === 'asr_result') {
      if (msg.is_final) {
        setLines((cur) => {
          const index = msg.seg_id ? cur.findIndex((line) => line.seg_id === msg.seg_id) : -1;
          const line = {
              seg_id: msg.seg_id,
              text: msg.text,
              ts: new Date().toLocaleTimeString('zh-CN', {hour12: false}),
              startMs: msg.start_ms ?? null,
              speakerId: msg.speaker_id,
              speakerLabel: msg.speaker_label,
            };
          if (index >= 0) return cur.map((item, i) => i === index ? {...item, ...line} : item);
          return cur.at(-1)?.text === msg.text ? cur : [...cur, line];
        });
        setPartial('');
        setPartialSpeaker(null);
      } else {
        setPartial((cur) => `${cur}${msg.text}`);
        setPartialSpeaker(msg.speaker_id || null);
      }
    }
    if (msg.type === 'meeting_result' && msg.final) {
      onEnded?.();
    }
    if (msg.type === 'error') onUnavailable?.(msg);
  };

  const connect = () => {
    const socket = new WebSocket(buildWsUrl(sessionId, token));
    ws.current = socket;
    socket.onopen = () => setConnected(true);
    socket.onmessage = handleMessage;
    socket.onclose = () => {
      setConnected(false);
      if (!dead.current) {
        reconnectTimer.current = setTimeout(connect, 4000);
      }
    };
  };

  useEffect(() => {
    dead.current = false;
    connect();
    return () => {
      dead.current = true;
      clearTimeout(reconnectTimer.current);
      ws.current?.close();
    };
  }, [sessionId]);

  const directory = buildSpeakerDirectory({speakers: result.speakers, speakerRoles: result.speaker_roles});
  const rolesById = new Map([...directory.values()].map((d) => [d.speakerId, d.roleLabel]));

  return (
    <div className={`app-root observer-view${embedded ? ' embedded' : ''}`} style={{height: '100%', display: 'flex', flexDirection: 'column'}}>
      {/* Header */}
      <div className="obs-header">
        {!embedded && <button className="btn ghost icon-btn" onClick={onBack}><IconBack /></button>}
        <div className="obs-title">
          <span className="obs-label">{embedded ? '录音卡录音中 · 网页只读' : '旁听中'}</span>
          <span className="obs-duration">{formatClock(elapsedSec)}</span>
        </div>
        <div className={`obs-status ${connected ? 'ok' : 'off'}`}>
          <IconSignal />{connected ? '实时' : '重连中…'}
        </div>
      </div>

      {/* Subtabs */}
      <div className="subtabs">
        {SUBTABS.map((t) => (
          <button key={t.id} className={subtab === t.id ? 'active' : ''} onClick={() => setSubtab(t.id)}>
            {t.icon}{t.label}
          </button>
        ))}
      </div>

      {/* Content */}
      <div className="scroll-area" style={{flex: 1, minHeight: 0}}>
        {subtab === 'captions' && (
          <div style={{padding: '0 0 16px'}}>
            <section className="panel">
              <div className="panel-head">
                <h2>实时字幕</h2>
                {connected && <span className="live-dot">直播中</span>}
              </div>
              <div className="panel-body">
                <CaptionStream
                  lines={lines}
                  partial={partial}
                  partialSpeaker={partialSpeaker}
                  recording={connected}
                  rolesById={rolesById}
                />
              </div>
            </section>
          </div>
        )}

      </div>
    </div>
  );
}
