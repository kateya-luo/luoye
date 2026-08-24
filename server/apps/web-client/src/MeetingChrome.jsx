import React from 'react';
import Waveform from './Waveform';
import {formatClock} from './summaryDerive';
import {
  IconMic, IconChevron, IconSignal, IconWifiOff, IconAlert, IconRefresh,
  IconCaptions, IconMinutes, IconCheck, IconSave, IconCamera, IconShield, IconBookmark,
} from './icons';

export function MeetingStatusBar({recording, paused, elapsedSec, title, microphones, microphoneId, onMicChange, online, level}) {
  return (
    <div className="statusbar">
      <div className={`rec-badge ${recording && !paused ? 'live' : ''}`}>
        <span className="pulse" />{recording ? (paused ? '已暂停' : '录音中') : '待开始'}
      </div>
      <div className={`rec-time ${recording ? '' : 'idle'}`}>{formatClock(elapsedSec)}</div>
      <div className="sb-divider" />
      <div className="meeting-title">{title || '新的会议'}<span className="sub">| Clear Meeting</span></div>
      <div className="sb-spacer" />
      <div className="sb-select no-drag">
        <IconMic />
        <select value={microphoneId} onChange={(e) => onMicChange(e.target.value)} disabled={recording}>
          <option value="">电脑麦克风（模拟录音笔）</option>
          {microphones.map((d, i) => <option key={d.deviceId} value={d.deviceId}>{d.label || `麦克风 ${i + 1}`}</option>)}
        </select>
        <IconChevron />
      </div>
      <div className={`sb-field ${online ? 'net-ok' : 'net-bad'}`}>
        {online ? <IconSignal /> : <IconWifiOff />}{online ? '网络正常' : '网络异常'}
      </div>
      <Waveform level={level} active={recording && !paused} />
    </div>
  );
}

export function OfflineBanner({offlineSec, attempt, nextRetrySec, queued}) {
  return (
    <>
      <div className="offline-banner">
        <IconAlert />
        网络连接中断，正在自动重连…
        <span className="off-time">已离线 {formatClock(offlineSec)}</span>
        <span className="spacer" />
        <button><IconChevron style={{transform: 'rotate(180deg)'}} /> 查看详情</button>
      </div>
    </>
  );
}

// Reconnect + upload-queue detail (shown in the right aside while offline).
export function ReconnectAside({offlineSec, attempt, nextRetrySec, queued, savedAt}) {
  return (
    <>
      <div className="panel-head"><h2>会议控制与摘要</h2><span className="meta">本地记录中</span></div>
      <div className="panel-body">
        <div className="cache-card">
          <div className="cc-head"><IconSave /> 本地缓存中</div>
          <div className="cc-stats"><span>已缓存 {queued.captions} 条字幕</span><span>{queued.updates} 纪要更新</span></div>
          <small>网络恢复后将自动同步到云端</small>
        </div>

        <div className="aside-sect">
          <div className="reconnect-card">
            <div className="reconnect-row"><IconRefresh className="spin" /> 第 {attempt} 次重连中{nextRetrySec ? ` · ${nextRetrySec} 秒后再次尝试` : ''}</div>
            <div className="rc-steps">
              <div className="rc-step done"><span className="tick"><IconCheck /></span>检测到网络中断</div>
              <div className="rc-step done"><span className="tick"><IconCheck /></span>开始自动重连</div>
              <div className="rc-step active"><span className="tick">{attempt}</span>第 {attempt} 次重连中<time>进行中</time></div>
              <div className="rc-step wait"><span className="tick" />重连成功<time>待定</time></div>
            </div>
          </div>
        </div>

        <div className="aside-sect">
          <div className="sect-head"><IconSave /> 待上传队列 <span className="count">恢复后自动补传</span></div>
          <div className="queue-item">
            <div className="qi-ic"><IconCaptions /></div>
            <div className="qi-main"><b>字幕缓存</b><span>{queued.captions} 条字幕</span></div>
            <div className="qi-size">{Math.max(1, Math.round(queued.captions * 1.3))} KB<div className="wait">等待上传</div></div>
          </div>
          <div className="queue-item">
            <div className="qi-ic"><IconMinutes /></div>
            <div className="qi-main"><b>纪要更新</b><span>{queued.updates} 条更新</span></div>
            <div className="qi-size">{Math.max(1, queued.updates * 4)} KB<div className="wait">等待上传</div></div>
          </div>
        </div>
      </div>
    </>
  );
}

export function FooterBar({recording, online, savedAt, markers}) {
  return (
    <div className="footerbar">
      <div className="fb-item ok"><IconCheck /> 本地自动保存中</div>
      {savedAt && <div className="fb-item">已保存 {savedAt}</div>}
      {markers > 0 && <div className="fb-item"><IconBookmark /> 已标记 {markers} 处重点</div>}
      <div className="spacer" />
      <div className="fb-item">{online ? '最小化后继续录音' : '恢复连接后自动同步'}</div>
      <div className="fb-item"><IconCamera /> 摄像头：未连接</div>
      <div className={`fb-item ${online ? '' : ''}`}><IconShield /> {online ? '自动备份（云端连接）' : '自愈中（网络监控）'}</div>
    </div>
  );
}
