import React, {useEffect, useRef, useState} from 'react';
import {MindMapPreview} from './MindMap';
import {keyConclusions} from './summaryDerive';
import {
  IconPlay, IconPause, IconStop, IconBookmark, IconCheckCircle, IconCircle, IconDot,
  IconTarget, IconClipboard, IconStar, IconMinutes, IconCopy, IconPlus, IconExpand, IconSignal,
} from './icons';

export function RollingMinutes({chapters, summary, onSeek}) {
  const endRef = useRef(null);
  const containerRef = useRef(null);
  const [scrollLocked, setScrollLocked] = useState(false);
  const scrollLockedRef = useRef(false);

  useEffect(() => {
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

  useEffect(() => {
    if (scrollLockedRef.current) return;
    const c = containerRef.current;
    if (c) c.scrollTop = c.scrollHeight;
    else endRef.current?.scrollIntoView({block: 'end', behavior: 'instant'});
  }, [chapters]);

  const scrollToBottom = () => {
    scrollLockedRef.current = false;
    setScrollLocked(false);
    endRef.current?.scrollIntoView({block: 'end', behavior: 'smooth'});
  };

  if (!chapters.length) {
    return (
      <div className="empty">
        <div className="e-ic"><IconMinutes /></div>
        <div>{summary || '会议结束且转写完整后，请选择模板生成会议纪要。'}</div>
      </div>
    );
  }
  return (
    <div>
      <div className="timeline">
        {chapters.map((c, i) => (
          <div className="tl-item" key={i}>
            <div className="tl-time">{c.time}</div>
            <div className="tl-card">
              <div className="tl-card-head">
                <h3>{c.title}</h3>
                <span className={`badge ${c.badge}`}>{c.badgeText}</span>
              </div>
              {c.items.length === 1
                ? <p>{c.items[0]}</p>
                : <ul>{c.items.map((it, j) => <li key={j}>{it}</li>)}</ul>}
              {(c.markCount > 0 || c.marks?.length > 0) && (
                <div className="tl-tags">
                  {(c.marks || []).map((mark) => (
                    <button type="button" className="tl-mark" key={mark.id}
                      onClick={() => onSeek?.(mark.at_ms)} disabled={!onSeek}>
                      MARK {Math.floor((mark.at_ms || 0) / 60000)}:{String(Math.floor(((mark.at_ms || 0) % 60000) / 1000)).padStart(2, '0')}
                    </button>
                  ))}
                  {!c.marks?.length && <span className="badge todo">MARK {c.markCount}</span>}
                </div>
              )}
            </div>
          </div>
        ))}
      </div>
      <div ref={endRef} />
      {scrollLocked && (
        <button className="caption-scroll-btn" onClick={scrollToBottom}>↓ 跳到最新</button>
      )}
    </div>
  );
}

function SpeakerRoles({directory}) {
  const speakers = [...(directory?.values() || [])].filter((s) => s.roleLabel || s.segmentCount);
  if (!speakers.length) return null;
  return (
    <div className="aside-sect">
      <div className="sect-head"><IconDot style={{color: 'var(--brand)'}} /> 说话人角色 <span className="count">{speakers.length}</span></div>
      {speakers.map((s) => (
        <div className="concl" key={s.speakerId}>
          <span className="spk-tag" style={{color: s.color.ink, background: s.color.soft}}>{s.label}</span>
          <div>
            <b style={{color: 'var(--ink)'}}>{s.roleLabel || '参与者'}</b>
            {s.description && <div className="muted" style={{fontSize: 12.5, marginTop: 2}}>{s.description}</div>}
          </div>
        </div>
      ))}
    </div>
  );
}

export function SummaryAside({result, stats, directory, updatedAt}) {
  const conclusions = keyConclusions(result);
  const todos = result?.action_items || [];
  const decisions = result?.decisions || [];
  return (
    <>
      <div className="panel-head"><h2>本场摘要</h2>{updatedAt && <span className="meta">更新于 {updatedAt}</span>}</div>
      <div className="panel-body">
        <div className="stat-row">
          <div className="stat"><b>{stats.chapters}</b><span>章节数</span></div>
          <div className="stat"><b>{stats.words.toLocaleString()}</b><span>字数</span></div>
          <div className="stat"><b style={{fontSize: 17}}>{stats.duration}</b><span>时长</span></div>
        </div>

        <div className="aside-sect">
          <div className="sect-head"><IconCheckCircle /> 关键结论</div>
          {conclusions.length ? conclusions.map((c, i) => (
            <div className="concl" key={i}>
              {c.kind === 'done' ? <IconCheckCircle className="c-done" /> : <IconCircle className="c-wait" />}
              <span>{c.text}</span>
            </div>
          )) : <p className="muted">暂未形成关键结论</p>}
        </div>

        <SpeakerRoles directory={directory} />

        <div className="aside-sect">
          <div className="sect-head"><IconClipboard /> 待办事项 {todos.length > 0 && <span className="count">{todos.length}</span>}</div>
          {todos.length ? todos.map((t, i) => (
            <div className="todo-item" key={i}>
              <span className="box" />
              <span className="who">{t.task}</span>
              <span className="meta"><span className="owner">{t.assignee}</span><span className="due">{t.deadline}</span></span>
            </div>
          )) : <p className="muted">暂未识别到待办事项</p>}
        </div>

        <div className="aside-sect">
          <div className="sect-head"><IconStar style={{color: 'var(--brand)'}} /> 决策记录 {decisions.length > 0 && <span className="count">{decisions.length}</span>}</div>
          {decisions.length ? decisions.map((d, i) => (
            <div className="decision" key={i}><span className="num">{i + 1}</span><span>{d}</span></div>
          )) : <p className="muted">暂未识别到明确决策</p>}
        </div>
      </div>
    </>
  );
}

function Controls({recording, paused, onStart, onStop, onPause, onMark}) {
  return (
    <div className="ctrl-grid">
      <button className="ctrl-btn" onClick={onStart} disabled={recording}><IconPlay /> 开始会议</button>
      <button className="ctrl-btn warn" onClick={onPause} disabled={!recording}><IconPause /> {paused ? '继续' : '暂停'}</button>
      <button className="ctrl-btn danger" onClick={onStop} disabled={!recording}><IconStop /> 结束会议</button>
      <button className="ctrl-btn" onClick={onMark} disabled={!recording}><IconBookmark /> 标记重点</button>
    </div>
  );
}

export function ControlAside({result, controls, updatedAt}) {
  const points = [
    ...(result?.decisions || []).map((text) => ({text, done: true})),
    ...(result?.action_items || []).map((t) => ({text: t.task, done: false})),
  ].slice(0, 6);
  return (
    <>
      <div className="panel-head"><h2>会议控制与摘要</h2></div>
      <div className="panel-body">
        <Controls {...controls} />

        <div className="aside-sect">
          <div className="sect-head"><IconCheckCircle /> 要点总结 {updatedAt && <span className="count" style={{marginLeft: 'auto', background: 'none', color: 'var(--muted)'}}>更新于 {updatedAt}</span>}</div>
          {points.length ? points.map((p, i) => (
            <div className="concl" key={i}>
              {p.done ? <IconCheckCircle className="c-done" /> : <IconCircle className="c-prog" />}
              <span>{p.text}</span>
            </div>
          )) : <p className="muted">识别到要点后在此汇总</p>}
        </div>

        <div className="aside-sect">
          <div className="sect-head"><IconExpand style={{color: 'var(--brand)'}} /> 思维导图预览</div>
          <MindMapPreview value={result?.mindmap} />
        </div>
      </div>
    </>
  );
}

// 旧版会议数据的兼容阶段标签。V0.21 录制期间不再生成纪要。
const STAGE_TAGS = {
  rolling: {label: '旧版实时纪要', bg: '#3b82f6'},
  draft: {label: '初稿·定稿中', bg: '#f59e0b'},
  gap_merged: {label: '已并入补录', bg: '#8b5cf6'},
  final: {label: '最终版', bg: '#10b981'},
};

export function SmartMinutes({result, chapters, updatedAt}) {
  const [inner, setInner] = useState('summary');
  const conclusions = keyConclusions(result);
  const todos = result?.action_items || [];
  const decisions = result?.decisions || [];
  const stageTag = STAGE_TAGS[result?.summary_stage];
  return (
    <>
      <div className="panel-head" style={{flexDirection: 'column', alignItems: 'flex-start', gap: 8}}>
        <div style={{display: 'flex', alignItems: 'center', justifyContent: 'space-between', width: '100%'}}>
          <h2 style={{display: 'flex', alignItems: 'center', gap: 8}}>智能纪要
            {stageTag && <span style={{fontSize: 12, fontWeight: 600, lineHeight: 1, color: '#fff',
              background: stageTag.bg, borderRadius: 10, padding: '3px 8px'}}>{stageTag.label}</span>}
          </h2>
          {updatedAt && <span className="meta">更新于 {updatedAt}</span>}
        </div>
        <div className="sm-inner-tabs">
          <button className={inner === 'summary' ? 'active' : ''} onClick={() => setInner('summary')}>智能纪要</button>
          <button className={inner === 'rolling' ? 'active' : ''} onClick={() => setInner('rolling')}>纪要要点</button>
        </div>
      </div>
      <div className="panel-body">
        {inner === 'summary' ? (
          <>
            <div className="aside-sect" style={{borderTop: 0}}>
              <div className="sect-head"><IconCheckCircle /> 关键结论</div>
              {conclusions.length ? conclusions.map((c, i) => (
                <div className="concl" key={i}><IconCheckCircle className="c-done" /><span>{c.text}</span></div>
              )) : <p className="muted">暂未形成关键结论</p>}
            </div>
            <div className="aside-sect">
              <div className="sect-head"><IconClipboard /> 待办事项 {todos.length > 0 && <span className="count">{todos.length}</span>}</div>
              {todos.length ? todos.map((t, i) => (
                <div className="todo-item" key={i}>
                  <span className="box" /><span className="who">{t.task}</span>
                  <span className="meta">
                    {!isPlaceholder(t.assignee) && <span className="owner">{t.assignee}</span>}
                    {!isPlaceholder(t.deadline) && <span className="due">{t.deadline}</span>}
                  </span>
                </div>
              )) : <p className="muted">暂未识别到待办事项</p>}
            </div>
            <div className="aside-sect">
              <div className="sect-head"><IconStar style={{color: 'var(--brand)'}} /> 决策记录 {decisions.length > 0 && <span className="count">{decisions.length}</span>}</div>
              {decisions.length ? decisions.map((d, i) => (
                <div className="decision" key={i}><span className="num">{i + 1}</span><span>{d}</span></div>
              )) : <p className="muted">暂未识别到明确决策</p>}
            </div>
          </>
        ) : (
          <RollingMinutes chapters={chapters} summary={result?.summary} />
        )}
      </div>
    </>
  );
}

const isPlaceholder = (v) => !v || /^(to be confirmed|待确认|tbd|n\/a|—|--)$/i.test(v.trim());

export function MeetingAssistAside({result, controls, stats, online, savedAt, speakerEnabled, onSpeakerToggle, translateTo, onTranslateChange}) {
  const {recording, paused, onStart, onStop, onPause, onMark} = controls;
  const todos = result?.action_items || [];
  const decisions = result?.decisions || [];
  return (
    <>
      <div className="panel-head"><h2>会议助手</h2></div>
      <div className="panel-body">
        <div className="ctrl-grid">
          <button className="ctrl-btn" onClick={onStart} disabled={recording}><IconPlay /> 开始会议</button>
          <button className="ctrl-btn warn" onClick={onPause} disabled={!recording}><IconPause /> {paused ? '继续' : '暂停'}</button>
          <button className="ctrl-btn danger" onClick={onStop} disabled={!recording}><IconStop /> 结束会议</button>
          <button className="ctrl-btn" onClick={onMark} disabled={!recording}><IconBookmark /> 标记重点</button>
        </div>
        <label className="assist-toggle" style={{opacity: recording ? 0.5 : 1, pointerEvents: recording ? 'none' : 'auto'}}>
          <span>说话人识别</span>
          <div className={`mct-switch ${speakerEnabled ? 'on' : ''}`} onClick={() => onSpeakerToggle?.(!speakerEnabled)}>
            <div className="mct-switch-thumb" />
          </div>
        </label>
        <label className="assist-toggle" style={{opacity: recording ? 0.5 : 1, pointerEvents: recording ? 'none' : 'auto'}}>
          <span>实时翻译</span>
          <select value={translateTo || ''} onChange={(e) => onTranslateChange?.(e.target.value)}
            style={{height: 30, borderRadius: 8, border: '1px solid var(--panel-3)', padding: '0 8px', fontSize: 13, background: 'var(--panel)', color: 'var(--ink)'}}>
            <option value="">关闭</option>
            <option value="en">译为英文</option>
            <option value="zh">译为中文</option>
            <option value="ja">译为日文</option>
            <option value="ko">译为韩文</option>
          </select>
        </label>
        <div className="assist-stats">
          <div className="astat"><b style={{fontSize: 17}}>{stats.duration}</b><span>时长</span></div>
          <div className="astat"><b>{stats.words.toLocaleString()}</b><span>字数</span></div>
          <div className="astat"><b>{decisions.length}</b><span>重点</span></div>
          <div className="astat"><b>{todos.length}</b><span>待办</span></div>
        </div>
        <div className="aside-sect">
          <div className="sect-head">状态</div>
          <div className="assist-status-row">
            <IconMinutes /><span>本地保存</span>
            <span className="asr-val ok">{savedAt ? '✓ 已保存' : '自动保存中'}</span>
          </div>
          <div className="assist-status-row">
            <IconSignal /><span>云端同步</span>
            <span className={`asr-val ${online ? 'ok' : 'err'}`}>{online ? '✓ 已连接' : '✗ 离线'}</span>
          </div>
        </div>
        <div className="aside-sect">
          <p className="muted" style={{fontSize: 12.5, lineHeight: 1.75, margin: 0}}>
            录制过程中只进行实时转写。会议结束并完成离线整理后，可在历史记录中选择模板，一次性生成正式纪要。
          </p>
        </div>
      </div>
    </>
  );
}

export function NodeDetailAside({node, result, controls, createdAt}) {
  return (
    <>
      <div className="panel-head"><h2>节点详情</h2></div>
      <div className="panel-body">
        <Controls {...controls} />

        <div className="aside-sect">
          <div className="sect-head">节点信息</div>
          {node ? (
            <div className="node-info">
              <div className="ni-title">
                <IconDot style={{color: node.color?.ink || 'var(--brand)'}} />
                {node.title}{node.root && <span className="muted" style={{fontWeight: 600, fontSize: 12}}>（根节点）</span>}
              </div>
              {node.root && <p>本次会议围绕「{node.title}」展开，下分各主题分支与要点。</p>}
              {node.branch && <p className="muted">所属分支：{node.branch}</p>}
              {Array.isArray(node.items) && node.items.length > 0 && (
                <ul style={{margin: '8px 0 0', paddingLeft: 18}}>
                  {node.items.map((it, i) => <li key={i} style={{fontSize: 13, color: 'var(--ink-2)', lineHeight: 1.6}}>{it}</li>)}
                </ul>
              )}
              {createdAt && <div className="ni-times"><span>创建时间：{createdAt}</span></div>}
            </div>
          ) : <p className="muted">点击左侧任意节点查看详情</p>}
        </div>

        <div className="aside-sect">
          <div className="sect-head">快速操作</div>
          <div className="qa-item"><IconExpand /> 在文档中展开此节点</div>
          <div className="qa-item"><IconCopy /> 复制节点内容</div>
          <div className="qa-item"><IconStar /> 添加为重点节点</div>
          <div className="qa-item"><IconPlus /> 添加子节点</div>
        </div>
      </div>
    </>
  );
}
