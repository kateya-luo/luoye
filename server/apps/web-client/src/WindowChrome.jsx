import React from 'react';
import {
  IconLive, IconHistory, IconSettings, IconCloudCheck, IconCloud, IconWifiOff,
  IconDownload, IconMin, IconMax, IconClose, IconClipboard,
} from './icons';

const isDesktop = Boolean(window.clearMeetingDesktop?.isDesktop);
const win = window.clearMeetingDesktop?.windowControls;

const TABS = [
  {id: 'recorder', label: '实时会议', icon: <IconLive />},
  {id: 'history', label: '会议历史', icon: <IconHistory />},
  {id: 'agenda', label: '议程待办', icon: <IconClipboard />},
  {id: 'settings', label: '设置', icon: <IconSettings />},
];

function CloudStatus({status}) {
  if (status === 'offline') return <div className="cloud-pill warn no-drag"><span className="dot" /><IconWifiOff /><span>网络异常</span></div>;
  if (status === 'connected') return <div className="cloud-pill no-drag"><span className="dot" /><IconCloudCheck /><span>云端已连接</span></div>;
  return <div className="cloud-pill off no-drag"><span className="dot" /><IconCloud /><span>未连接</span></div>;
}

export default function WindowChrome({minimal = false, tab, onTab, cloudStatus = 'idle', account, onLogout}) {
  const initial = (account || '会').trim().slice(0, 1).toUpperCase();
  return (
    <div className="titlebar">
      <div className="brand"><span className="brand-mark">C</span><span className="brand-name">CLEAR MEETING</span></div>

      {!minimal && (
        <div className="nav-tabs no-drag">
          {TABS.map((t) => (
            <button key={t.id} className={tab === t.id ? 'active' : ''} onClick={() => onTab(t.id)}>
              {t.icon}<span>{t.label}</span>
            </button>
          ))}
        </div>
      )}

      <div className="titlebar-right">
        {!minimal && <>
          <CloudStatus status={cloudStatus} />
          <button className="user-chip no-drag" onClick={onLogout} title="点击退出登录">
            <span className="avatar">{initial}<span className="presence" /></span>
            {account || '本地账户'}
          </button>
          <button className="icon-btn no-drag" title="下载导出"><IconDownload /></button>
        </>}

        {isDesktop && win && (
          <div className="win-ctrls no-drag">
            <button onClick={() => win.minimize()} title="最小化"><IconMin /></button>
            <button onClick={() => win.maximize()} title="最大化"><IconMax /></button>
            <button className="close" onClick={() => win.close()} title="关闭"><IconClose /></button>
          </div>
        )}
      </div>
    </div>
  );
}
