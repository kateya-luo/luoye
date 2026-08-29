export function formatProcessingEta(status) {
  if (!status) return '';
  if (['uploading', 'waiting_upload'].includes(status.stage) && !status.eta_upper_seconds) {
    return '上传耗时取决于当前网络，传完后会自动继续';
  }
  const lower = Number(status.eta_lower_seconds || 0);
  const upper = Number(status.eta_upper_seconds || 0);
  let label = '';
  if (upper > 0) {
    if (upper < 60) label = '预计不到 1 分钟';
    else {
      const lowMinutes = Math.max(1, Math.floor(lower / 60));
      const highMinutes = Math.max(lowMinutes, Math.ceil(upper / 60));
      label = lowMinutes === highMinutes
        ? `预计约 ${highMinutes} 分钟`
        : `预计约 ${lowMinutes}–${highMinutes} 分钟`;
    }
  }
  const ahead = Number(status.queue_ahead_jobs || 0);
  if (ahead > 0) {
    const queue = `前方还有 ${ahead} 个任务`;
    return label ? `${label}（${queue}，实际等待可能增加）` : queue;
  }
  return label;
}

export function processingStatusLabel(status) {
  if (!status) return '已完成';
  if (['failed', 'stalled'].includes(status.stage)) return '处理异常';
  if (status.active && status.stage === 'uploading') return '上传中';
  if (status.active && status.stage === 'waiting_upload') return '等待补传';
  if (status.active) return '后台处理中';
  return '转写已完成';
}
