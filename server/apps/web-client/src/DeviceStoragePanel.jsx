import React, {useCallback, useEffect, useState} from 'react';
import {createDeviceStorageCommand, getDeviceStorage} from './api';
import {IconRefresh, IconTrash} from './icons';

const bytes = (value) => {
  const amount = Number(value || 0);
  if (amount >= 1024 ** 3) return `${(amount / 1024 ** 3).toFixed(1)} GB`;
  if (amount >= 1024 ** 2) return `${(amount / 1024 ** 2).toFixed(1)} MB`;
  return `${Math.round(amount / 1024)} KB`;
};

const localTime = (epoch) => epoch
  ? new Date(Number(epoch) * 1000).toLocaleString()
  : '本地录音';

export default function DeviceStoragePanel({device}) {
  const [open, setOpen] = useState(false);
  const [storage, setStorage] = useState(null);
  const [busy, setBusy] = useState('');
  const [message, setMessage] = useState('');

  const load = useCallback(async () => {
    setBusy('load');
    try {
      setStorage(await getDeviceStorage(device.device_id));
      setMessage('');
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy('');
    }
  }, [device.device_id]);

  useEffect(() => {
    if (!open) return undefined;
    load();
    const timer = window.setInterval(load, 10000);
    return () => window.clearInterval(timer);
  }, [open, load]);

  const queue = async (action, ids = []) => {
    setBusy(ids[0] || action);
    try {
      await createDeviceStorageCommand(device.device_id, action, ids);
      setMessage(device.online
        ? 'SD 删除命令已发送到设备。'
        : '设备当前离线；命令会在设备上线后执行。');
      await load();
    } catch (error) {
      setMessage(error.message);
    } finally {
      setBusy('');
    }
  };

  const deleteAllLocal = async () => {
    const count = storage?.sessions?.length || 0;
    const amount = (storage?.sessions || []).reduce(
      (total, session) => total + Number(session.local_bytes || 0), 0);
    if (!window.confirm(
      `确定删除设备 SD 卡中的全部历史录音吗？\n\n共 ${count} 条，约 ${bytes(amount)}。\n只删除 SD 卡文件，不会删除云端会议、字幕、纪要或待办。`,
    )) return;
    await queue('delete_all_closed');
  };

  if (!open) {
    return <button className="btn ghost storage-open" type="button" onClick={() => setOpen(true)}>管理设备 SD 卡</button>;
  }

  const total = Number(storage?.total_bytes || 0);
  const free = Number(storage?.free_bytes || 0);
  const usedPercent = total ? Math.min(100, Math.round((total - free) * 100 / total)) : 0;
  const pending = storage?.commands?.find((item) => ['queued', 'in_progress'].includes(item.status));

  return (
    <section className="device-storage">
      <div className="storage-head">
        <div>
          <strong>固定式 SD 存储</strong>
          <span>这里只管理设备本地文件，与云端会议历史完全独立。</span>
        </div>
        <div>
          <button className="btn ghost" type="button" disabled={busy === 'load'} onClick={load}><IconRefresh />刷新</button>
          <button className="btn ghost" type="button" onClick={() => setOpen(false)}>收起</button>
        </div>
      </div>
      {!storage ? <div className="device-empty">正在读取设备 SD 卡清单…</div> : <>
        <div className="storage-capacity">
          <div><span>已用 {bytes(total - free)}</span><strong>可用 {bytes(free)} / {bytes(total)}</strong></div>
          <div className="storage-bar"><i style={{width: `${usedPercent}%`}} /></div>
          <small>{storage.scan_complete
            ? `清单更新：${storage.scanned_at ? new Date(storage.scanned_at).toLocaleString() : '等待设备上报'}`
            : '设备正在分批上报本地文件清单'}</small>
        </div>
        <div className="storage-policy">
          <button className="btn ghost danger" type="button"
            disabled={Boolean(pending) || busy === 'delete_all_closed' || !(storage.sessions || []).length}
            onClick={deleteAllLocal}><IconTrash />一键删除全部历史录音</button>
        </div>
        {pending && <div className="storage-notice">已有 SD 命令等待设备执行：{
          pending.action === 'delete_sessions' ? '删除指定文件' : '删除全部历史录音'
        }</div>}
        {message && <div className="storage-notice">{message}</div>}
        <div className="storage-sessions">
          {(storage.sessions || []).length === 0
            ? <div className="device-empty">SD 卡中没有历史录音。</div>
            : storage.sessions.map((session) => (
              <div className="storage-session" key={session.client_session_id}>
                <div><strong>{localTime(session.ended_at_utc)}</strong><span>{bytes(session.local_bytes)}</span></div>
                <button className="btn ghost danger" type="button"
                  title="只删除设备 SD 卡中的这份文件"
                  disabled={Boolean(pending) || busy === session.client_session_id}
                  onClick={() => queue('delete_sessions', [session.client_session_id])}>
                  <IconTrash />删除 SD 文件
                </button>
              </div>
            ))}
        </div>
      </>}
    </section>
  );
}
