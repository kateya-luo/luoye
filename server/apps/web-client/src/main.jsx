import React, {useEffect, useRef, useState} from 'react';
import {createRoot} from 'react-dom/client';

// ── 前端日志收集：把 console.error / 未捕获错误 发到服务端 ──
(function setupClientLog() {
  const send = (level, msg) => {
    try {
      reportClientLog(level, msg).catch(() => {});
    } catch {}
  };
  const origError = console.error.bind(console);
  console.error = (...args) => { origError(...args); send('error', args.map(String).join(' ')); };
  const origWarn = console.warn.bind(console);
  console.warn = (...args) => { origWarn(...args); send('warn', args.map(String).join(' ')); };
  window.addEventListener('error', (e) => send('onerror', `${e.message} @ ${e.filename}:${e.lineno}\n${e.error?.stack || ''}`));
  window.addEventListener('unhandledrejection', (e) => send('unhandledrejection', `${e.reason?.message || e.reason}\n${e.reason?.stack || ''}`));
})();
import {clearToken, getAuthStatus, getCurrentUser, getStoredToken, listActiveSessions, login, reportClientLog} from './api';
import WindowChrome from './WindowChrome';
import MeetingView from './MeetingView';
import HistoryPanel from './HistoryPanel';
import SettingsPanel from './SettingsPanel';
import AgendaPanel from './AgendaPanel';
import ObserverView from './ObserverView';
import LoginScreen, {loadLoginPrefs} from './LoginScreen';
import {IconLive, IconHistory, IconSettings, IconControl, IconClipboard} from './icons';
import './styles.css';

function MobileBottomNav({tab, onTab, hasLive}) {
  const TABS = [
    {id: 'control', label: '控制', icon: <IconControl />},
    {id: 'recorder', label: '录音', icon: <IconLive />},
    {id: 'history', label: '历史', icon: <IconHistory />},
    {id: 'agenda', label: '议程', icon: <IconClipboard />},
    {id: 'settings', label: '设置', icon: <IconSettings />},
  ];
  return (
    <nav className="mobile-bottom-nav">
      {TABS.map((t) => (
        <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => onTab(t.id)}>
          <div style={{position: 'relative', display: 'inline-flex'}}>
            {t.icon}
            {t.id === 'history' && hasLive && <span className="lsb-badge" />}
          </div>
          {t.label}
        </button>
      ))}
    </nav>
  );
}

async function fetchActiveSessions() {
  try {
    return await listActiveSessions();
  } catch {
    return [];
  }
}

function App() {
  const [authLoading, setAuthLoading] = useState(true);
  const [authenticated, setAuthenticated] = useState(false);
  const [authRequired, setAuthRequired] = useState(false);
  const [token, setToken] = useState(getStoredToken());
  const [currentUser, setCurrentUser] = useState(null);
  const [tab, setTab] = useState(() => window.matchMedia('(max-width: 480px)').matches ? 'control' : 'recorder');
  const [historyRefresh, setHistoryRefresh] = useState(0);
  const [cloud, setCloud] = useState('idle');
  const [activeSessions, setActiveSessions] = useState([]);
  const [globalError, setGlobalError] = useState(null);
  const freshDeviceSession = activeSessions.find((session) => session.source === 'device') || null;
  const [activeDeviceSession, setActiveDeviceSession] = useState(null);
  const autoOpenedDevice = useRef(null);

  useEffect(() => {
    const onErr = (e) => setGlobalError(`[JS] ${e.message}\n${e.filename}:${e.lineno}\n${e.error?.stack || ''}`);
    const onRej = (e) => setGlobalError(`[Promise] ${e.reason?.message || e.reason}\n${e.reason?.stack || ''}`);
    window.addEventListener('error', onErr);
    window.addEventListener('unhandledrejection', onRej);
    return () => { window.removeEventListener('error', onErr); window.removeEventListener('unhandledrejection', onRej); };
  }, []);

  useEffect(() => {
    getAuthStatus(token)
      .then((status) => {
        setAuthRequired(status.required);
        setAuthenticated(status.authenticated);
        setCurrentUser(status.user || null);
        setCloud(status.authenticated ? 'connected' : 'idle');
      })
      .catch(() => setAuthenticated(false))
      .finally(() => setAuthLoading(false));
    if (window.location.protocol !== 'file:') navigator.serviceWorker?.register('/sw.js').catch(() => {});
  }, []);

  // Poll for active recording sessions so mobile users can join as observers.
  useEffect(() => {
    if (!authenticated) return;
    const poll = () => fetchActiveSessions().then(setActiveSessions);
    poll();
    const t = setInterval(poll, 10000);
    return () => clearInterval(t);
  }, [authenticated, token]);

  // Once a real live packet proves that a card meeting is active, keep the
  // workspace read-only through pauses and temporary network loss. Only the
  // card's final result (or an explicit server error) releases the workspace.
  useEffect(() => {
    if (freshDeviceSession) setActiveDeviceSession(freshDeviceSession);
  }, [freshDeviceSession?.session_id]);

  // A recorder-card meeting owns the recording workspace. Open it once when
  // it appears; later tab changes remain under the user's control.
  useEffect(() => {
    const next = activeDeviceSession?.session_id || null;
    if (next && autoOpenedDevice.current !== next) {
      autoOpenedDevice.current = next;
      setTab('recorder');
    }
    if (!next) autoOpenedDevice.current = null;
  }, [activeDeviceSession?.session_id]);

  const recordingRef = useRef(false);
  const [logoutWarning, setLogoutWarning] = useState(false);

  // 所有 Hooks 必须在任何条件返回之前调用，否则 globalError 从空变为非空时会触发
  // “Rendered fewer hooks than expected”，反而让错误页本身崩溃。
  if (globalError) return (
    <div style={{padding: 16, background: '#fff', color: '#c00', fontSize: 12, wordBreak: 'break-all', whiteSpace: 'pre-wrap', position: 'fixed', inset: 0, overflow: 'auto', zIndex: 9999}}>
      <b>崩溃错误（截图发开发者）：</b>{'\n\n'}{globalError}
    </div>
  );

  const handleLogin = async (username, password) => {
    const next = await login(username, password);
    setToken(next); setAuthenticated(true); setCloud('connected');
    getCurrentUser().then(setCurrentUser).catch(() => setCurrentUser({username}));
  };
  const doLogout = () => { clearToken(); setToken(''); setCurrentUser(null); setAuthenticated(false); setCloud('idle'); };
  const handleLogout = () => {
    if (recordingRef.current) { setLogoutWarning(true); return; }
    doLogout();
  };

  if (authLoading) return <div className="app-root"><div className="app-loading">正在连接 Clear Meeting…</div></div>;
  if (!authenticated) return <LoginScreen onLogin={handleLogin} />;

  const account = currentUser?.username || loadLoginPrefs().account || '';
  return (
    <div className="app-root">
      <WindowChrome tab={tab} onTab={setTab} cloudStatus={cloud} account={account}
        onLogout={authRequired ? handleLogout : undefined} />

      <div style={{flex: 1, minHeight: 0, display: 'flex', flexDirection: 'column'}}>
        {/* MeetingView 在录音/控制两个tab下都保持挂载，避免切换tab时断开录音 */}
        <div style={{display: (tab === 'recorder' || tab === 'control') ? 'flex' : 'none', flex: 1, flexDirection: 'column', minHeight: 0}}>
          {activeDeviceSession ? (
            <ObserverView
              embedded
              sessionId={activeDeviceSession.session_id}
              sessionInfo={activeDeviceSession}
              token={token}
              onEnded={() => {
                setActiveDeviceSession(null);
                setHistoryRefresh((value) => value + 1);
              }}
              onUnavailable={() => setActiveDeviceSession(null)}
            />
          ) : (
            <MeetingView token={token} onCloud={setCloud} onMeetingSaved={() => setHistoryRefresh((v) => v + 1)} controlMode={tab === 'control'} onRecordingChange={(r) => { recordingRef.current = r; }} />
          )}
        </div>
        {tab === 'history' && <HistoryPanel refreshKey={historyRefresh} onUnauthorized={handleLogout} />}
        {tab === 'agenda' && <AgendaPanel />}
        {tab === 'settings' && <SettingsPanel onLogout={handleLogout}
          onPasswordChanged={(nextToken) => setToken(nextToken)} />}
      </div>

      <MobileBottomNav tab={tab} onTab={setTab} hasLive={false} />

      {logoutWarning && (
        <div className="modal-overlay" onClick={() => setLogoutWarning(false)}>
          <div className="modal-box" onClick={(e) => e.stopPropagation()}>
            <h3>正在录音中</h3>
            <p>当前正在进行录音，退出账号将丢失本次会议内容，确认退出？</p>
            <div className="modal-actions">
              <button className="btn ghost" onClick={() => setLogoutWarning(false)}>继续录音</button>
              <button className="btn primary" style={{background: 'var(--red)'}} onClick={() => { setLogoutWarning(false); doLogout(); }}>确认退出</button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

createRoot(document.getElementById('root')).render(<App />);
