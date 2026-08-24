const {contextBridge, ipcRenderer} = require('electron');

// Minimal bridge for the always-on-top floating widget. It only renders meeting
// state pushed from the main process and forwards a couple of commands back.
contextBridge.exposeInMainWorld('clearMeetingFloat', {
  onState: (listener) => {
    if (typeof listener !== 'function') throw new TypeError('listener 必须是函数');
    const handler = (_event, state) => listener(state);
    ipcRenderer.on('float-state', handler);
    return () => ipcRenderer.removeListener('float-state', handler);
  },
  open: () => ipcRenderer.send('tray-command', 'open'),
  pause: () => ipcRenderer.send('tray-command', 'pause'),
});
