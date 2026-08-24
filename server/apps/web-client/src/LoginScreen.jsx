import React, {useState} from 'react';
import {testConnection} from './api';
import WindowChrome from './WindowChrome';
import {
  IconCaptions, IconMinutes, IconMindmap, IconServer, IconUser, IconLock,
  IconEye, IconEyeOff, IconShield, IconSignal, IconSave, IconLogin, IconCloudCheck, IconCheck,
} from './icons';

const PREFS_KEY = 'clear-meeting-login-prefs';
const loadPrefs = () => { try { return JSON.parse(localStorage.getItem(PREFS_KEY)) || {}; } catch { return {}; } };
const savePrefs = (prefs) => localStorage.setItem(PREFS_KEY, JSON.stringify(prefs));

const isDesktop = Boolean(window.clearMeetingDesktop?.isDesktop);
const configuredServer = window.clearMeetingDesktop?.serverUrl || '';

const FEATURES = [
  {icon: <IconCaptions />, title: '实时字幕', desc: 'AI 实时语音转文字，准确稳定，多语言支持。'},
  {icon: <IconMinutes />, title: '滚动纪要', desc: '智能提炼要点，滚动生成纪要，不错过任何关键信息。'},
  {icon: <IconMindmap />, title: '思维导图', desc: '一键生成思维导图，梳理观点结构，激发团队思考。'},
];

export default function LoginScreen({onLogin}) {
  const prefs = loadPrefs();
  const [server, setServer] = useState(configuredServer || prefs.server || (isDesktop ? '' : location.origin));
  const [account, setAccount] = useState(prefs.account || '');
  const [password, setPassword] = useState('');
  const [showPwd, setShowPwd] = useState(false);
  const [rememberServer, setRememberServer] = useState(prefs.rememberServer ?? true);
  const [autoLaunch, setAutoLaunch] = useState(prefs.autoLaunch ?? false);
  const [rememberLogin, setRememberLogin] = useState(prefs.rememberLogin ?? true);
  const [autoReconnect, setAutoReconnect] = useState(prefs.autoReconnect ?? true);
  const [error, setError] = useState('');
  const [busy, setBusy] = useState(false);
  const [conn, setConn] = useState(configuredServer ? {ok: true} : null);

  const persist = () => savePrefs({
    server: rememberServer ? server : '', account, rememberServer, autoLaunch, rememberLogin, autoReconnect,
    lastLogin: rememberLogin ? new Date().toISOString() : prefs.lastLogin,
  });

  const test = async () => {
    setError(''); setConn({testing: true});
    try { await testConnection(server); setConn({ok: true, at: new Date()}); }
    catch (e) { setConn({ok: false, message: e.message}); }
  };

  const submit = async (event) => {
    event.preventDefault();
    setBusy(true); setError('');
    try {
      persist();
      // Desktop: if the server changed, persist it (relaunches into the configured app).
      if (isDesktop && server && server.replace(/\/$/, '') !== configuredServer.replace(/\/$/, '')) {
        await window.clearMeetingDesktop.saveServerUrl(server);
        return;
      }
      await onLogin(account, password);
    } catch (loginError) {
      setError(loginError.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login-shell">
      <WindowChrome minimal />
      <form className="login-body" onSubmit={submit}>
        <div className="login-card">
          <div className="login-left">
            <div className="login-logo"><span className="brand-mark">C</span><b>CLEAR MEETING</b></div>
            <p className="tagline">让每一次会议更高效、更清晰</p>
            {FEATURES.map((f) => (
              <div className="feature" key={f.title}>
                <div className="f-ic">{f.icon}</div>
                <div><b>{f.title}</b><p>{f.desc}</p></div>
              </div>
            ))}
            <div className="footnote">
              <a href="#">{isDesktop ? 'Windows Client' : 'Web Client'} V0.19.2</a>
              <div>© 2026 CLEAR MEETING. All rights reserved.</div>
            </div>
          </div>

          <div className="login-right">
            <div className="field">
              <label><IconServer className="hint-i" /> 云服务器地址</label>
              <div className="input">
                <IconServer />
                <input value={server} onChange={(e) => setServer(e.target.value)}
                       placeholder="wss://meeting.example.com/ws" autoComplete="url"
                       />
              </div>
            </div>

            <div className="field">
              <label><IconUser className="hint-i" /> 账号 / 邮箱</label>
              <div className="input">
                <IconUser />
                <input value={account} onChange={(e) => setAccount(e.target.value)}
                       placeholder="you@example.com" autoComplete="username" />
              </div>
            </div>

            <div className="field">
              <label><IconLock className="hint-i" /> 密码</label>
              <div className="input">
                <IconLock />
                <input type={showPwd ? 'text' : 'password'} value={password}
                       onChange={(e) => setPassword(e.target.value)} placeholder="访问密码"
                       autoComplete="current-password" autoFocus />
                <button type="button" className="eye" onClick={() => setShowPwd((v) => !v)} tabIndex={-1}>
                  {showPwd ? <IconEyeOff /> : <IconEye />}
                </button>
              </div>
            </div>

            <div className="opt-row">
              <label className="checkbox"><input type="checkbox" checked={rememberServer} onChange={(e) => setRememberServer(e.target.checked)} />记住服务器地址</label>
              <label className="checkbox"><input type="checkbox" checked={autoLaunch} onChange={(e) => setAutoLaunch(e.target.checked)} />开机自动启动</label>
              <label className="checkbox"><input type="checkbox" checked={rememberLogin} onChange={(e) => setRememberLogin(e.target.checked)} />记住登录状态</label>
              <label className="checkbox"><input type="checkbox" checked={autoReconnect} onChange={(e) => setAutoReconnect(e.target.checked)} />网络异常自动重连</label>
            </div>

            <div className="login-note"><IconShield /> 登录信息加密保存在本机</div>

            {error && <div className="form-error">{error}</div>}

            <div className="login-actions">
              <button type="button" className="btn ghost" onClick={test}><IconSignal /> 测试连接</button>
              <button type="button" className="btn ghost" onClick={persist}><IconSave /> 保存设置</button>
              <button type="submit" className="btn primary lg" disabled={busy || !account.trim() || !password}>
                <IconLogin /> {busy ? '正在登录…' : '登录进入客户端'}
              </button>
            </div>

            {conn && (
              <div className="status-box">
                <div className="sb-cloud"><IconCloudCheck /></div>
                <div className="sb-lines">
                  <div>{conn.testing ? '连接测试：测试中…' : conn.ok
                    ? <><IconCheck /> 连接测试：<b>成功</b></>
                    : <span style={{color: 'var(--red)'}}>连接测试：失败 · {conn.message}</span>}</div>
                  {prefs.lastLogin && <div><IconCheck /> 上次登录：{new Date(prefs.lastLogin).toLocaleString('zh-CN', {hour12: false})}</div>}
                  {conn.ok && <div><IconCheck /> 当前状态：<b>可连接云端</b></div>}
                </div>
              </div>
            )}

            {location.protocol === 'http:' && !isDesktop && (
              <small className="security-warning">当前为 HTTP 联调环境，请只使用专用临时密码。配置域名后将升级为 HTTPS。</small>
            )}
          </div>
        </div>
      </form>
    </div>
  );
}

export {loadPrefs as loadLoginPrefs};
