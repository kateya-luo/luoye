export const MINUTES_REFRESH_WARNING = '纪要已生成并保存；历史状态刷新暂时失败，稍后重新打开会议即可。';

export async function applyMinutesJobResponse(current, {
  setJob, setShowPicker, onMinutesReady, onChanged, setError,
} = {}) {
  setJob?.(current);
  if (current?.state === 'ready') {
    setShowPicker?.(false);
    if (current.result) onMinutesReady?.(current.result);
    let refreshed = true;
    try { refreshed = (await onChanged?.()) !== false; }
    catch { refreshed = false; }
    setError?.(refreshed ? '' : MINUTES_REFRESH_WARNING);
    return true;
  }
  if (current?.state === 'failed') {
    setError?.(current.last_error || '生成失败');
    return true;
  }
  return false;
}
