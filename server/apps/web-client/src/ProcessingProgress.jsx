import React from 'react';
import {formatProcessingEta} from './processingStatus';

export default function ProcessingProgress({status, compact = false, showCompleted = false}) {
  if (!status || (!status.active && !['failed', 'stalled'].includes(status.stage) && !showCompleted)) return null;
  const percent = Number.isFinite(status.progress_percent) ? status.progress_percent : null;
  const uploadStage = ['uploading', 'waiting_upload'].includes(status.stage);
  const eta = formatProcessingEta(status);
  return (
    <div className={`processing-progress ${compact ? 'compact' : ''} stage-${status.stage || 'unknown'}`}>
      <div className="processing-head">
        <span className={`processing-indicator ${status.active ? 'active' : ''}`} />
        <b>{status.title || '后台处理中'}</b>
        {percent != null && <span className="processing-percent">{percent}%</span>}
      </div>
      {percent != null && (
        <div className="processing-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow={percent}>
          <i style={{width: `${Math.max(0, Math.min(100, percent))}%`}} />
        </div>
      )}
      {!compact && status.detail && <div className="processing-detail">{status.detail}</div>}
      {eta && <div className="processing-eta">{eta}</div>}
      {!compact && status.jobs?.total > 0 && (
        <div className="processing-facts">
          <span>{uploadStage ? '已接收部分转写' : '后台任务'} {status.jobs.done || 0}/{status.jobs.total}</span>
          {status.audio_duration_ms > 0 && <span>录音约 {Math.max(1, Math.round(status.audio_duration_ms / 60000))} 分钟</span>}
          <span>页面可关闭，服务器会继续处理</span>
        </div>
      )}
    </div>
  );
}
