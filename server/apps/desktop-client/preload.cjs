const {contextBridge, ipcRenderer} = require('electron');

const serverUrlArgument = process.argv.find((value) => value.startsWith('--clear-meeting-server='));
const serverUrl = serverUrlArgument?.slice('--clear-meeting-server='.length) || '';

contextBridge.exposeInMainWorld('clearMeetingDesktop', {
  isDesktop: true,
  platform: process.platform,
  serverUrl,
  saveServerUrl: (url) => ipcRenderer.invoke('save-server-url', url),
  changeServer: () => ipcRenderer.invoke('change-server'),
  windowControls: {
    minimize: () => ipcRenderer.send('window-control', 'minimize'),
    maximize: () => ipcRenderer.send('window-control', 'maximize'),
    close: () => ipcRenderer.send('window-control', 'close'),
  },
  // 录音状态上报（驱动"录音中"桌面悬浮窗）：{title, micOn, recording, paused, elapsedLabel}
  reportMeetingState: (state) => ipcRenderer.send('meeting-state', state),
  bleCommand: (command) => ipcRenderer.invoke('ble-command', command),
  onBleEvent: (listener) => {
    if (typeof listener !== 'function') throw new TypeError('listener 必须是函数');
    const handler = (_event, message) => listener(message);
    ipcRenderer.on('ble-event', handler);
    return () => ipcRenderer.removeListener('ble-event', handler);
  },
});
