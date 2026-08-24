const {app, BrowserWindow, Menu, Tray, ipcMain, shell, nativeImage, screen} = require('electron');
const fs = require('node:fs');
const path = require('node:path');
const {BleAgent} = require('./ble-agent.cjs');

const configPath = path.join(app.getPath('userData'), 'config.json');

function readConfig() {
  try { return JSON.parse(fs.readFileSync(configPath, 'utf8')); }
  catch { return {}; }
}

function normalizeServerUrl(raw) {
  const value = String(raw || '').trim();
  if (!value) throw new Error('请输入服务器地址');
  const withProtocol = /^[a-z]+:\/\//i.test(value) ? value : `http://${value}`;
  const url = new URL(withProtocol);
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('地址必须使用 http:// 或 https://');
  url.pathname = '/'; url.search = ''; url.hash = '';
  return url.toString().replace(/\/$/, '');
}

function sameOrigin(url, expectedOrigin) {
  try { return new URL(url).origin === expectedOrigin; }
  catch { return false; }
}

let config = readConfig();
let bleAgent;
let mainWindow = null;
let floatWindow = null;
let tray = null;
let isQuitting = false;
let lastMeetingState = {recording: false};

// 录音中的桌面悬浮窗：无边框透明、置顶、不占任务栏，录音时显示、非录音隐藏。
// 状态由网页层通过 reportMeetingState 上报 → 'meeting-state' → 转发给悬浮窗 float.html。
function createFloatWindow() {
  if (floatWindow && !floatWindow.isDestroyed()) return floatWindow;
  const {width: sw, height: sh} = screen.getPrimaryDisplay().workAreaSize;
  floatWindow = new BrowserWindow({
    width: 306, height: 116, x: sw - 322, y: sh - 136,
    frame: false, transparent: true, resizable: false, movable: true,
    alwaysOnTop: true, skipTaskbar: true, show: false, maximizable: false, minimizable: false,
    webPreferences: {
      preload: path.join(__dirname, 'app', 'float-preload.cjs'),
      contextIsolation: true, nodeIntegration: false, sandbox: true,
    },
  });
  floatWindow.setAlwaysOnTop(true, 'screen-saver');   // 盖在任务栏/其它窗之上
  floatWindow.setVisibleOnAllWorkspaces(true);
  floatWindow.loadFile(path.join(__dirname, 'app', 'float.html'));
  floatWindow.on('closed', () => { floatWindow = null; });
  return floatWindow;
}

function pushFloatState() {
  const fw = createFloatWindow();
  if (!fw || fw.isDestroyed()) return;
  const send = () => { if (!fw.isDestroyed()) fw.webContents.send('float-state', lastMeetingState); };
  if (fw.webContents.isLoading()) fw.webContents.once('did-finish-load', send); else send();
  if (lastMeetingState.recording) { if (!fw.isVisible()) fw.showInactive(); }
  else if (fw.isVisible()) fw.hide();
}

// 托盘图标（内嵌 base64，蓝圆+白点录音意象，无外部文件依赖）
const TRAY_ICON = nativeImage.createFromDataURL('data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAYAAABzenr0AAAAyElEQVR42u2XzQ2GIAyGncmrV2ZgBubwzlbuwAws4KlJLQknQxQK2ObL1+S5+MP7CrWUZflHQ2z7aQhPHEQkIBPztXTPzBB2RCCwkvSsGyG85i9DJundlStu8/RiJ2kMyxHHwdiWaYcJBqBqOTrX/DUnarIdJ+OeDIQPDISnIlM90D0aTZiSAc8RZhrx7OQbZOAoGYi94g0mYskAfGgAVBoQXwLxJBT/DWULkXgpVrEZiW/HKhoS8ZZMRVOqoi1XcTBRczT76bgArm4aeuwci9YAAAAASUVORK5CYII=');

function showMainWindow() {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  } else {
    createWindow();
  }
}

function createTray() {
  if (tray) return;
  tray = new Tray(TRAY_ICON);
  tray.setToolTip('Clear Meeting — 关闭窗口后仍在后台运行（录音不中断）');
  tray.setContextMenu(Menu.buildFromTemplate([
    {label: '显示主窗口', click: showMainWindow},
    {type: 'separator'},
    {label: '退出 Clear Meeting', click: () => { isQuitting = true; app.quit(); }},
  ]));
  tray.on('click', showMainWindow);
  tray.on('double-click', showMainWindow);
}
if (config.serverUrl) {
  try {
    const origin = new URL(config.serverUrl).origin;
    if (origin.startsWith('http://')) app.commandLine.appendSwitch('unsafely-treat-insecure-origin-as-secure', origin);
  } catch { config = {}; }
}

function createWindow() {
  const window = new BrowserWindow({
    width: 1360,
    height: 900,
    minWidth: 960,
    minHeight: 640,
    title: 'Clear Meeting',
    backgroundColor: '#eef2f9',
    frame: false,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'),
      additionalArguments: config.serverUrl ? [`--clear-meeting-server=${config.serverUrl}`] : [],
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });

  const configuredOrigin = config.serverUrl ? new URL(config.serverUrl).origin : null;
  window.webContents.session.setPermissionRequestHandler((webContents, permission, callback, details) => {
    const origin = (() => { try { return new URL(details.requestingUrl).origin; } catch { return ''; } })();
    callback(permission === 'media' && (origin === configuredOrigin || details.requestingUrl.startsWith('file:')));
  });
  window.webContents.setWindowOpenHandler(({url}) => {
    if (configuredOrigin && sameOrigin(url, configuredOrigin)) return {action: 'allow'};
    shell.openExternal(url); return {action: 'deny'};
  });
  window.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith('file:') && configuredOrigin && !sameOrigin(url, configuredOrigin)) event.preventDefault();
  });

  // 点 X 关闭 → 最小化到托盘（录音/上传继续），不退出；只有托盘"退出"或应用退出流程才真正关闭
  window.on('close', (event) => {
    if (!isQuitting) {
      event.preventDefault();
      window.hide();
    }
  });

  if (config.serverUrl) window.loadFile(path.join(__dirname, 'app', 'client', 'index.html'));
  else window.loadFile(path.join(__dirname, 'app', 'setup.html'));
  mainWindow = window;
}

ipcMain.handle('save-server-url', (event, raw) => {
  if (!event.senderFrame.url.startsWith('file:')) throw new Error('仅允许在设置页修改服务器');
  const serverUrl = normalizeServerUrl(raw);
  fs.mkdirSync(path.dirname(configPath), {recursive: true});
  fs.writeFileSync(configPath, JSON.stringify({serverUrl}, null, 2), {mode: 0o600});
  app.relaunch(); app.exit(0);
});

// 网页层上报录音状态 → 驱动悬浮窗显隐与内容
ipcMain.on('meeting-state', (event, state) => {
  lastMeetingState = state || {recording: false};
  pushFloatState();
});
// 悬浮窗上的"打开主界面"按钮
ipcMain.on('tray-command', (event, cmd) => { if (cmd === 'open') showMainWindow(); });

ipcMain.on('window-control', (event, action) => {
  const window = BrowserWindow.fromWebContents(event.sender);
  if (!window) return;
  if (action === 'minimize') window.minimize();
  else if (action === 'maximize') window.isMaximized() ? window.unmaximize() : window.maximize();
  else if (action === 'close') window.close();
});

ipcMain.handle('change-server', () => {
  try { fs.unlinkSync(configPath); } catch {}
  app.relaunch(); app.exit(0);
});

ipcMain.handle('ble-command', (event, command) => {
  const senderUrl = event.senderFrame.url;
  const allowedOrigin = config.serverUrl ? new URL(config.serverUrl).origin : null;
  const isSetup = senderUrl.startsWith('file:');
  if (!isSetup && (!allowedOrigin || !sameOrigin(senderUrl, allowedOrigin))) {
    throw new Error('BLE 命令来源未授权');
  }
  if (!command || typeof command.command !== 'string') throw new Error('BLE 命令格式无效');
  bleAgent.send(command);
  return {accepted: true};
});

app.whenReady().then(() => {
  bleAgent = new BleAgent({
    app,
    onEvent: (message) => {
      for (const window of BrowserWindow.getAllWindows()) {
        if (!window.isDestroyed()) window.webContents.send('ble-event', message);
      }
    },
  });
  bleAgent.start();
  Menu.setApplicationMenu(Menu.buildFromTemplate([{
    label: 'Clear Meeting',
    submenu: [
      {label: '更换服务器', click: () => { try { fs.unlinkSync(configPath); } catch {} app.relaunch(); app.exit(0); }},
      {type: 'separator'},
      {role: 'quit', label: '退出'},
    ],
  }, {role: 'viewMenu', label: '视图'}]));
  createTray();
  createFloatWindow();   // 预建（隐藏），录音开始即可秒显、无加载延迟
  createWindow();
  app.on('activate', () => { if (BrowserWindow.getAllWindows().length === 0) createWindow(); });
});

app.on('before-quit', () => { isQuitting = true; bleAgent?.stop(); });
app.on('window-all-closed', () => { if (process.platform !== 'darwin' && isQuitting) app.quit(); });
