import React, {useCallback, useEffect, useState} from 'react';
import {
  claimDevice,
  getSpeakerBackendHealth,
  getCurrentUser,
  listDevices,
  renameDevice,
  updateDeviceSpeakerMode,
  unbindDevice,
} from './api';
import {IconPlus, IconRefresh, IconSave, IconSignal, IconTrash, IconUser} from './icons';
import DeviceStoragePanel from './DeviceStoragePanel';

const formatTime = (value) => {
  if (!value) return '尚未上线';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
};

const deviceName = (device) => device.display_name || '我的落叶';

export default function DeviceManagementPanel() {
  const [devices, setDevices] = useState([]);
  const [account, setAccount] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [pairingCode, setPairingCode] = useState('');
  const [newDeviceName, setNewDeviceName] = useState('');
  const [claiming, setClaiming] = useState(false);
  const [editingId, setEditingId] = useState('');
  const [editingName, setEditingName] = useState('');
  const [savingId, setSavingId] = useState('');
  const [speakerSavingId, setSpeakerSavingId] = useState('');
  const [speakerBackend, setSpeakerBackend] = useState(null);
  const [unbindTarget, setUnbindTarget] = useState(null);
  const [unbinding, setUnbinding] = useState(false);

  const refresh = useCallback(async ({quiet = false} = {}) => {
    if (!quiet) setRefreshing(true);
    try {
      const [nextDevices, nextAccount, nextSpeakerBackend] = await Promise.all([
        listDevices(), getCurrentUser(), getSpeakerBackendHealth(),
      ]);
      setDevices(nextDevices);
      setAccount(nextAccount);
      setSpeakerBackend(nextSpeakerBackend);
      setError('');
    } catch (requestError) {
      if (!quiet) setError(requestError.message);
    } finally {
      setLoading(false);
      if (!quiet) setRefreshing(false);
    }
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(() => refresh({quiet: true}), 15000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  const submitClaim = async (event) => {
    event.preventDefault();
    const code = pairingCode.replace(/\D/g, '');
    setError(''); setSuccess('');
    if (code.length !== 6) {
      setError('请输入录音笔屏幕上显示的 6 位配对码');
      return;
    }
    setClaiming(true);
    try {
      const device = await claimDevice(code, newDeviceName);
      setPairingCode(''); setNewDeviceName('');
      setSuccess(`已绑定 ${deviceName(device)}，录音笔会自动领取设备凭据。`);
      await refresh({quiet: true});
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setClaiming(false);
    }
  };

  const startRename = (device) => {
    setEditingId(device.device_id);
    setEditingName(deviceName(device));
    setError(''); setSuccess('');
  };

  const saveRename = async (device) => {
    const name = editingName.trim();
    if (!name) { setError('设备名称不能为空'); return; }
    setSavingId(device.device_id);
    setError(''); setSuccess('');
    try {
      const updated = await renameDevice(device.device_id, name);
      setDevices((current) => current.map((item) => item.device_id === device.device_id ? {...item, ...updated} : item));
      setEditingId('');
      setSuccess('设备名称已更新。');
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setSavingId('');
    }
  };

  const confirmUnbind = async () => {
    if (!unbindTarget) return;
    setUnbinding(true);
    setError(''); setSuccess('');
    try {
      await unbindDevice(unbindTarget.device_id);
      setDevices((current) => current.filter((item) => item.device_id !== unbindTarget.device_id));
      setSuccess(`${deviceName(unbindTarget)} 已解绑，设备上的旧令牌已经失效。`);
      setUnbindTarget(null);
    } catch (requestError) {
      setError(requestError.message);
    } finally {
      setUnbinding(false);
    }
  };

  const setSpeakerMode = async (device, enabled) => {
    setSpeakerSavingId(device.device_id); setError(''); setSuccess('');
    try {
      const updated = await updateDeviceSpeakerMode(device.device_id, enabled);
      setDevices((items) => items.map((item) => item.device_id === device.device_id ? {...item, ...updated} : item));
      setSuccess(`多人语音识别已${enabled ? '开启' : '关闭'}，从下一场会议生效。`);
    } catch (requestError) { setError(requestError.message); }
    finally { setSpeakerSavingId(''); }
  };

  return (
    <>
      <div className="set-card device-management-card">
        <div className="device-section-head">
          <div>
            <h3>我的设备</h3>
            <p className="desc">录音笔绑定后，其录音、议程和语音待办只属于当前账号。</p>
          </div>
          <button className="btn ghost device-refresh" type="button" disabled={refreshing} onClick={() => refresh()}>
            <IconRefresh />{refreshing ? '刷新中' : '刷新'}
          </button>
        </div>

        {account && (
          <div className="device-account-line">
            <IconUser /><span>当前绑定账号</span><strong>{account.username || account.name || account.id}</strong>
          </div>
        )}

        <form className="device-claim-form" onSubmit={submitClaim}>
          <div className="device-claim-copy">
            <strong>绑定新录音笔</strong>
            <span>让录音笔进入配网/绑定模式，再输入屏幕上的一次性配对码。</span>
          </div>
          <label>
            <span>6 位配对码</span>
            <input
              aria-label="6 位配对码"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={6}
              placeholder="例如 123456"
              value={pairingCode}
              onChange={(event) => setPairingCode(event.target.value.replace(/\D/g, '').slice(0, 6))}
            />
          </label>
          <label>
            <span>设备名称（可选）</span>
            <input maxLength={40} placeholder="例如 会议室落叶" value={newDeviceName}
              onChange={(event) => setNewDeviceName(event.target.value)} />
          </label>
          <button className="btn primary" type="submit" disabled={claiming || pairingCode.length !== 6}>
            <IconPlus />{claiming ? '正在绑定…' : '确认绑定'}
          </button>
        </form>

        {error && <div className="form-error device-feedback">{error}</div>}
        {success && <div className="form-success device-feedback">{success}</div>}

        <div className="device-list-head">
          <strong>已绑定设备</strong><span>{devices.length} 台</span>
        </div>

        {loading ? (
          <div className="device-empty">正在读取设备…</div>
        ) : devices.length === 0 ? (
          <div className="device-empty">当前账号还没有绑定录音笔。</div>
        ) : (
          <div className="device-list">
            {devices.map((device) => {
              const editing = editingId === device.device_id;
              const batteryKnown = device.battery_percent !== null && device.battery_percent !== undefined;
              return (
                <article className="device-card" key={device.device_id}>
                  <div className="device-card-main">
                    <div className={`device-online-mark ${device.online ? 'online' : ''}`}><IconSignal /></div>
                    <div className="device-identity">
                      {editing ? (
                        <div className="device-rename-row">
                          <input autoFocus maxLength={40} value={editingName}
                            onChange={(event) => setEditingName(event.target.value)}
                            onKeyDown={(event) => {
                              if (event.key === 'Enter') { event.preventDefault(); saveRename(device); }
                              if (event.key === 'Escape') setEditingId('');
                            }} />
                          <button className="btn primary" type="button" disabled={savingId === device.device_id} onClick={() => saveRename(device)}>
                            <IconSave />保存
                          </button>
                          <button className="btn ghost" type="button" onClick={() => setEditingId('')}>取消</button>
                        </div>
                      ) : (
                        <div className="device-name-row">
                          <strong>{deviceName(device)}</strong>
                          <span className={`device-status ${device.online ? 'online' : ''}`}>{device.online ? '在线' : '离线'}</span>
                        </div>
                      )}
                      <code>{device.device_id}</code>
                    </div>
                    {!editing && <button className="btn ghost device-rename" type="button" onClick={() => startRename(device)}>重命名</button>}
                  </div>

                  <dl className="device-facts">
                    <div><dt>绑定账号</dt><dd>{account?.username || account?.name || '当前账号'}</dd></div>
                    <div><dt>电量</dt><dd>{batteryKnown ? `${Math.round(device.battery_percent)}%` : '暂无数据'}</dd></div>
                    <div><dt>固件</dt><dd>{device.firmware_version || '未知'}</dd></div>
                    <div><dt>硬件版本</dt><dd>{device.hardware_revision || '未知'}</dd></div>
                    <div><dt>最后在线</dt><dd>{formatTime(device.last_seen_at)}</dd></div>
                    <div><dt>绑定代次</dt><dd>{device.binding_generation ?? '—'}</dd></div>
                  </dl>

                  <div className={`device-speaker-card ${device.speaker_diarization_enabled !== false ? 'enabled' : ''}`}>
                    <div className="device-speaker-visual" aria-hidden="true">
                      <span className="speaker-wave wave-left" />
                      <span className="speaker-person">人</span>
                      <span className="speaker-wave wave-right" />
                    </div>
                    <div className="device-speaker-copy">
                      <div className="device-speaker-title">
                        <strong>多人语音识别</strong>
                        <span className={`speaker-service-state ${speakerBackend?.ready && speakerBackend?.mode === 'remote' ? 'ready' : 'unavailable'}`}>
                          <i />{speakerBackend?.ready && speakerBackend?.mode === 'remote' ? '声纹服务正常' : '声纹服务不可用'}
                        </span>
                      </div>
                      <p>自动区分会议中的不同说话人，并在字幕与纪要中保留身份标签。</p>
                      <small>{speakerBackend?.ready && speakerBackend?.mode === 'remote'
                        ? '修改会从下一场会议开始生效'
                        : (speakerBackend?.detail || '请先检查云端声纹服务')}</small>
                    </div>
                    <button type="button" role="switch" aria-label="多人语音识别"
                      aria-checked={device.speaker_diarization_enabled !== false}
                      className={`speaker-toggle ${device.speaker_diarization_enabled !== false ? 'on' : ''}`}
                      disabled={speakerSavingId === device.device_id || !(speakerBackend?.ready && speakerBackend?.mode === 'remote')}
                      onClick={() => setSpeakerMode(device, device.speaker_diarization_enabled === false)}>
                      <span className="speaker-toggle-track"><i /></span>
                      <b>{speakerSavingId === device.device_id ? '保存中' : (device.speaker_diarization_enabled !== false ? '已开启' : '已关闭')}</b>
                    </button>
                  </div>

                  <DeviceStoragePanel device={device} />

                  <div className="device-card-foot">
                    <span>绑定于 {formatTime(device.bound_at)}</span>
                    <button className="btn ghost danger" type="button" onClick={() => setUnbindTarget(device)}>
                      <IconTrash />解绑设备
                    </button>
                  </div>
                </article>
              );
            })}
          </div>
        )}
      </div>

      {unbindTarget && (
        <div className="modal-overlay" onClick={() => !unbinding && setUnbindTarget(null)}>
          <div className="modal-box" onClick={(event) => event.stopPropagation()}>
            <h3>解绑 {deviceName(unbindTarget)}？</h3>
            <p>解绑会立即撤销这台录音笔的设备令牌。已有会议仍保留在当前账号中，录音笔需要重新配对后才能继续同步。</p>
            <div className="modal-actions">
              <button className="btn ghost" type="button" disabled={unbinding} onClick={() => setUnbindTarget(null)}>取消</button>
              <button className="btn primary" type="button" disabled={unbinding}
                style={{background: 'var(--red)'}} onClick={confirmUnbind}>
                {unbinding ? '正在解绑…' : '确认解绑'}
              </button>
            </div>
          </div>
        </div>
      )}
    </>
  );
}
