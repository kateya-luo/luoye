import React, {useState} from 'react';
import DeviceManagementPanel from './DeviceManagementPanel';
import {loadLoginPrefs} from './LoginScreen';
import {changePassword} from './api';
import {IconServer, IconShield} from './icons';

const isDesktop = Boolean(window.clearMeetingDesktop?.isDesktop);
const configuredServer = window.clearMeetingDesktop?.serverUrl || '';

export default function SettingsPanel({onLogout, onPasswordChanged}) {
  const prefs = loadLoginPrefs();
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [currentPassword, setCurrentPassword] = useState('');
  const [newPassword, setNewPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [passwordError, setPasswordError] = useState('');
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordSaved, setPasswordSaved] = useState(false);
  const changeServer = () => {
    // main process menu also offers this; here we just clear + relaunch via saveServerUrl flow.
    if (window.clearMeetingDesktop?.changeServer) window.clearMeetingDesktop.changeServer();
  };
  const closePassword = () => {
    if (passwordBusy) return;
    setPasswordOpen(false);
    setCurrentPassword(''); setNewPassword(''); setConfirmPassword(''); setPasswordError('');
  };
  const submitPassword = async (event) => {
    event.preventDefault();
    setPasswordError(''); setPasswordSaved(false);
    if (newPassword.length < 8) { setPasswordError('新密码至少需要 8 个字符'); return; }
    if (newPassword !== confirmPassword) { setPasswordError('两次输入的新密码不一致'); return; }
    setPasswordBusy(true);
    try {
      const nextToken = await changePassword(currentPassword, newPassword);
      onPasswordChanged?.(nextToken);
      setPasswordOpen(false);
      setCurrentPassword(''); setNewPassword(''); setConfirmPassword(''); setPasswordError('');
      setPasswordSaved(true);
    } catch (error) {
      setPasswordError(error.message);
    } finally {
      setPasswordBusy(false);
    }
  };
  return (
    <div className="settings-wrap">
      <div className="settings-inner">
        <div className="set-card">
          <h3>连接</h3>
          <p className="desc">客户端通过 WebSocket / TLS 连接云端，进行实时转写与纪要生成。</p>
          <div className="set-row">
            <label><IconServer style={{width: 15, height: 15, verticalAlign: -2, marginRight: 4}} />云服务器</label>
            <div className="input"><input value={configuredServer || prefs.server || '（同源）'} readOnly /></div>
          </div>
          <div className="set-row">
            <label>账号 / 邮箱</label>
            <div className="input"><input value={prefs.account || '本地账户'} readOnly /></div>
          </div>
          {isDesktop && (
            <div className="set-row">
              <label>更换服务器</label>
              <div><button className="btn ghost" onClick={changeServer}>更换并重新登录</button></div>
            </div>
          )}
        </div>

        <DeviceManagementPanel />

        <div className="set-card">
          <h3>偏好</h3>
          <p className="desc">登录时记住的本机偏好设置。</p>
          <div className="set-row"><label>记住服务器地址</label><div>{prefs.rememberServer ? '已开启' : '已关闭'}</div></div>
          <div className="set-row"><label>记住登录状态</label><div>{prefs.rememberLogin ? '已开启' : '已关闭'}</div></div>
          <div className="set-row"><label>网络异常自动重连</label><div>{prefs.autoReconnect ?? true ? '已开启' : '已关闭'}</div></div>
          <div className="set-row"><label>开机自动启动</label><div>{prefs.autoLaunch ? '已开启' : '已关闭'}</div></div>
        </div>

        <div className="set-card">
          <h3>账户</h3>
          <p className="desc"><IconShield style={{width: 14, height: 14, verticalAlign: -2}} /> 登录信息加密保存在本机。</p>
          {passwordSaved && <div className="form-success">密码修改成功，其他设备需要重新登录。</div>}
          <div className="account-actions">
            <button className="btn ghost" onClick={() => { setPasswordSaved(false); setPasswordOpen(true); }}>修改密码</button>
          <button className="btn ghost" style={{color: 'var(--red)'}} onClick={onLogout}>退出登录</button>
          </div>
        </div>
      </div>

      {passwordOpen && (
        <div className="modal-overlay" onClick={closePassword}>
          <form className="modal-box password-modal" onSubmit={submitPassword} onClick={(e) => e.stopPropagation()}>
            <h3>修改密码</h3>
            <p>修改当前账号的登录密码。成功后，其他设备上的旧登录会立即失效。</p>
            <label>当前密码<input type="password" value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)} autoComplete="current-password" autoFocus /></label>
            <label>新密码<input type="password" value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)} autoComplete="new-password"
              minLength={8} maxLength={128} placeholder="至少 8 个字符" /></label>
            <label>确认新密码<input type="password" value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)} autoComplete="new-password"
              minLength={8} maxLength={128} /></label>
            {passwordError && <div className="form-error">{passwordError}</div>}
            <div className="modal-actions">
              <button type="button" className="btn ghost" disabled={passwordBusy} onClick={closePassword}>取消</button>
              <button type="submit" className="btn primary" disabled={passwordBusy || !currentPassword || !newPassword || !confirmPassword}>
                {passwordBusy ? '正在保存…' : '确认修改'}
              </button>
            </div>
          </form>
        </div>
      )}
    </div>
  );
}
