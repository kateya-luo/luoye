import React, {useEffect, useLayoutEffect, useRef, useState} from 'react';
import {speakerColor} from './speakers';

function SpeakerTag({speakerId, label, role, pending}) {
  const color = speakerColor(speakerId);
  // 仅实时（未定稿）行在缺少说话人时显示「识别中」；已定稿但无说话人的行不再误显
  if (!label) return pending ? <span className="spk-role">识别中</span> : null;
  return (
    <>
      <span className="spk-tag" style={{color: color.ink, background: color.soft}}>{label}</span>
      {role && <span className="spk-role">{role}</span>}
    </>
  );
}

export default function CaptionStream({lines, partial, partialSpeaker, recording, rolesById, autoScroll = true, onSeek, activeSeekMs}) {
  const endRef = useRef(null);
  const containerRef = useRef(null);
  const [scrollLocked, setScrollLocked] = useState(false);
  const scrollLockedRef = useRef(false);

  useLayoutEffect(() => {
    const el = endRef.current;
    if (!el) return;
    let c = el.parentElement;
    while (c && c !== document.body) {
      const ov = getComputedStyle(c).overflowY;
      if (ov === 'auto' || ov === 'scroll') break;
      c = c.parentElement;
    }
    if (!c || c === document.body) return;
    containerRef.current = c;

    const onScroll = () => {
      const dist = c.scrollHeight - c.scrollTop - c.clientHeight;
      const locked = dist > 60;
      scrollLockedRef.current = locked;
      setScrollLocked(locked);
    };
    c.addEventListener('scroll', onScroll, {passive: true});
    return () => c.removeEventListener('scroll', onScroll);
  }, []);

  useLayoutEffect(() => {
    if (!autoScroll || scrollLockedRef.current) return;
    const c = containerRef.current;
    if (c) c.scrollTop = c.scrollHeight;
  }, [lines, partial, autoScroll]);

  const scrollToBottom = () => {
    scrollLockedRef.current = false;
    setScrollLocked(false);
    const c = containerRef.current;
    if (c) c.scrollTo({top: c.scrollHeight, behavior: 'smooth'});
  };

  // P0：按会议时间偏移排序（为补洞插入打基础）。仅当所有行都带 startMs 时才排，
  // 避免把旧的「纯转写无时间戳」历史记录打乱顺序。稳定排序保证同 startMs 维持插入序。
  const allTimed = lines.length > 0 && lines.every((l) => typeof l.startMs === 'number');
  const ordered = allTimed ? [...lines].sort((a, b) => a.startMs - b.startMs) : lines;

  const hasContent = lines.length || partial;
  if (!hasContent) {
    return (
      <div className="empty">
        <div className="e-ic">●</div>
        <div>开始录音后，实时字幕会出现在这里</div>
      </div>
    );
  }

  return (
    <div>
      {ordered.map((line, i) => {
        if (line.state === 'filling') {
          return (
            <div className="caption filling" key={line.seg_id ?? i} style={{opacity: 0.7}}>
              <div className="cap-text" style={{color: 'var(--muted)', fontStyle: 'italic'}}>⏳ 此段网络中断，补传转写中…</div>
            </div>
          );
        }
        const isLast = i === ordered.length - 1 && !partial;
        const isActive = activeSeekMs != null && line.startMs != null
          && activeSeekMs >= line.startMs && (ordered[i + 1]?.startMs == null || activeSeekMs < ordered[i + 1].startMs);
        return (
          <div
            className={`caption ${isLast && !onSeek ? 'active' : ''} ${isActive ? 'active' : ''} ${onSeek ? 'seekable' : ''}`}
            key={line.seg_id ?? i}
            onClick={onSeek && line.startMs != null ? () => onSeek(line.startMs) : undefined}
          >
            <div className="cap-head">
              <time>{line.ts}</time>
              <SpeakerTag speakerId={line.speakerId} label={line.speakerLabel} role={rolesById?.get(line.speakerId)} />
            </div>
            <div className="cap-text">{line.text}</div>
            {line.translation && (
              <div className="cap-translation" style={{marginTop: 4, paddingLeft: 8, borderLeft: '2px solid var(--accent, #3b82f6)', color: 'var(--ink-2, #667)', fontSize: '0.95em'}}>{line.translation}</div>
            )}
          </div>
        );
      })}
      {partial && (
        <div className="caption partial active">
          <div className="cap-head">
            <time>识别中</time>
            <SpeakerTag speakerId={partialSpeaker} label={partialSpeaker ? undefined : ''} role={rolesById?.get(partialSpeaker)} pending />
          </div>
          <div className="cap-text">{partial}<i className="blink">▍</i></div>
        </div>
      )}
      {recording && (
        <div className="caption-foot">
          <span className="eq"><i/><i/><i/></span>
          实时识别中 · 语音清晰度良好
        </div>
      )}
      <div ref={endRef} />

      {scrollLocked && autoScroll && (
        <button className="caption-scroll-btn" onClick={scrollToBottom}>
          ↓ 跳到最新
        </button>
      )}
    </div>
  );
}
