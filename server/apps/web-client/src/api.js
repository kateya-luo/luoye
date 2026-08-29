const TOKEN_KEY = 'ai-recorder-session-token';
const API_ORIGIN = window.clearMeetingDesktop?.serverUrl?.replace(/\/$/, '') || '';
const API_V1 = '/api/v1';
const DEVICE_API_V2 = '/api/v2';

export const getStoredToken = () => localStorage.getItem(TOKEN_KEY) || '';
export const storeToken = (token) => localStorage.setItem(TOKEN_KEY, token);
export const clearToken = () => localStorage.removeItem(TOKEN_KEY);

const RETRYABLE_GET_STATUSES = new Set([502, 503, 504]);
const GET_RETRY_DELAYS_MS = [250, 750];
const wait = (milliseconds) => new Promise((resolve) => setTimeout(resolve, milliseconds));

async function request(path, {token = getStoredToken(), ...options} = {}) {
  const headers = new Headers(options.headers || {});
  if (token) headers.set('Authorization', `Bearer ${token}`);
  if (options.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json');
  const method = String(options.method || 'GET').toUpperCase();
  const retryDelays = method === 'GET' ? GET_RETRY_DELAYS_MS : [];
  let response;
  for (let attempt = 0; ; attempt += 1) {
    try {
      response = await fetch(`${API_ORIGIN}${path}`, {...options, headers});
    } catch (error) {
      if (attempt >= retryDelays.length) throw error;
      await wait(retryDelays[attempt]);
      continue;
    }
    if (!RETRYABLE_GET_STATUSES.has(response.status) || attempt >= retryDelays.length) break;
    await wait(retryDelays[attempt]);
  }
  if (!response.ok) {
    let message = `请求失败（${response.status}）`;
    let code = '';
    try {
      const payload = await response.json();
      const detail = payload?.detail;
      message = payload?.error?.message
        || (typeof detail === 'string' ? detail : detail?.message)
        || message;
      code = payload?.error?.code || detail?.code || '';
    } catch { /* ignore non-JSON errors */ }
    const error = new Error(message);
    error.status = response.status;
    error.code = code;
    throw error;
  }
  return response;
}

export async function getAuthStatus(token = getStoredToken()) {
  if (!token) return {required: true, authenticated: false, user: null};
  try {
    const payload = await (await request(`${API_V1}/me`, {token})).json();
    return {required: true, authenticated: true, user: payload.user || payload};
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      return {required: true, authenticated: false, user: null};
    }
    throw error;
  }
}

export async function getCurrentUser() {
  const payload = await (await request(`${API_V1}/me`)).json();
  return payload.user || payload;
}

export async function getSpeakerBackendHealth() {
  try {
    const response = await fetch(`${API_ORIGIN}/health/ready`, {method: 'GET'});
    const payload = await response.json();
    return payload?.speaker || {ready: false, detail: `HTTP ${response.status}`};
  } catch {
    return {ready: false, detail: '无法连接声纹服务'};
  }
}

// Convert any server input (ws://, wss://, http://, https://, host[:port], with optional /ws)
// into an HTTP(S) origin suitable for REST calls.
export function toHttpOrigin(input) {
  let value = String(input || '').trim();
  if (!value) throw new Error('请输入服务器地址');
  value = value.replace(/^ws:\/\//i, 'http://').replace(/^wss:\/\//i, 'https://');
  if (!/^https?:\/\//i.test(value)) value = `http://${value}`;
  const url = new URL(value);
  return url.origin;
}

// Lightweight reachability check for the login screen "测试连接" button.
export async function testConnection(serverInput) {
  const origin = toHttpOrigin(serverInput);
  let response;
  try {
    // /me returning 401 is enough to prove that the v1 API is reachable.
    response = await fetch(`${origin}${API_V1}/me`, {method: 'GET'});
  } catch {
    throw new Error('无法连接到服务器，请检查地址与网络');
  }
  if (!response.ok && response.status !== 401 && response.status !== 403) {
    throw new Error(`服务器返回 ${response.status}`);
  }
  return true;
}

export async function login(username, password) {
  const payload = await (await request(`${API_V1}/auth/login`, {
    method: 'POST',
    token: '',
    body: JSON.stringify({username: String(username || '').trim(), password}),
  })).json();
  const token = payload.token || payload.access_token;
  if (!token) throw new Error('服务器未返回登录令牌');
  storeToken(token);
  return token;
}

export async function changePassword(currentPassword, newPassword) {
  const payload = await (await request(`${API_V1}/auth/change-password`, {
    method: 'POST',
    body: JSON.stringify({current_password: currentPassword, new_password: newPassword}),
  })).json();
  const token = payload.token || payload.access_token || getStoredToken();
  storeToken(token);
  return token;
}

export async function listDevices() {
  const payload = await (await request(`${DEVICE_API_V2}/me/devices`)).json();
  return Array.isArray(payload) ? payload : (payload.devices || []);
}

export async function claimDevice(pairingCode, displayName = '') {
  const body = {pairing_code: String(pairingCode || '').replace(/\D/g, '')};
  const name = String(displayName || '').trim();
  if (name) body.display_name = name;
  const payload = await (await request(`${DEVICE_API_V2}/me/devices/claim`, {
    method: 'POST',
    body: JSON.stringify(body),
  })).json();
  return payload.device || payload;
}

export async function renameDevice(deviceId, displayName) {
  const payload = await (await request(`${DEVICE_API_V2}/me/devices/${encodeURIComponent(deviceId)}`, {
    method: 'PATCH',
    body: JSON.stringify({display_name: String(displayName || '').trim()}),
  })).json();
  return payload.device || payload;
}

export async function updateDeviceSpeakerMode(deviceId, enabled) {
  const payload = await (await request(`${DEVICE_API_V2}/me/devices/${encodeURIComponent(deviceId)}`, {
    method: 'PATCH', body: JSON.stringify({speaker_diarization_enabled: Boolean(enabled)}),
  })).json();
  return payload.device || payload;
}

export async function unbindDevice(deviceId) {
  return (await request(`${DEVICE_API_V2}/me/devices/${encodeURIComponent(deviceId)}/binding`, {
    method: 'DELETE',
  })).json();
}

export async function getDeviceStorage(deviceId) {
  return (await request(`${DEVICE_API_V2}/me/devices/${encodeURIComponent(deviceId)}/storage`)).json();
}

export async function createDeviceStorageCommand(deviceId, action, sessionIds = []) {
  return (await request(`${DEVICE_API_V2}/me/devices/${encodeURIComponent(deviceId)}/storage/commands`, {
    method: 'POST',
    body: JSON.stringify({action, session_ids: sessionIds}),
  })).json();
}

export async function listActiveSessions() {
  const payload = await (await request(`${API_V1}/sessions/active`)).json();
  return Array.isArray(payload) ? payload : (payload.sessions || []);
}

export async function reportClientLog(level, message, userAgent = navigator.userAgent) {
  await request(`${API_V1}/client-log`, {
    method: 'POST',
    body: JSON.stringify({level, msg: String(message).slice(0, 3000), ua: userAgent}),
  });
}

export async function listMeetings() {
  return (await request(`${API_V1}/meetings`)).json();
}

export async function getMeeting(sessionId) {
  return (await request(`${API_V1}/meetings/${encodeURIComponent(sessionId)}`)).json();
}

export async function getMeetingProcessing(sessionId, token = getStoredToken()) {
  return (await request(`${API_V1}/meetings/${encodeURIComponent(sessionId)}/processing`, {token})).json();
}

export async function deleteMeeting(sessionId) {
  return (await request(`${API_V1}/meetings/${encodeURIComponent(sessionId)}`, {method: 'DELETE'})).json();
}

export async function updateMeetingTitle(sessionId, title) {
  return (await request(`${API_V1}/meetings/${encodeURIComponent(sessionId)}/title`, {
    method: 'PATCH',
    body: JSON.stringify({title}),
  })).json();
}

export async function listMinutesTemplates() {
  return (await request(`${API_V1}/minutes/templates`)).json();
}

export async function createMinutesJob(sessionId, templateId, templateVersion = 1) {
  return (await request(`${API_V1}/meetings/${encodeURIComponent(sessionId)}/minutes/jobs`, {
    method: 'POST', body: JSON.stringify({template_id: templateId, template_version: templateVersion, output_language: 'zh'}),
  })).json();
}

export async function getMinutesJob(jobId) {
  return (await request(`${API_V1}/minutes/jobs/${encodeURIComponent(jobId)}`)).json();
}

export async function listMeetingSpeakers(sessionId) {
  return (await request(`${API_V1}/meetings/${encodeURIComponent(sessionId)}/speakers`)).json();
}

export async function updateMeetingSpeaker(sessionId, speakerId, payload) {
  return (await request(`${API_V1}/meetings/${encodeURIComponent(sessionId)}/speakers/${encodeURIComponent(speakerId)}`, {
    method: 'PATCH', body: JSON.stringify(payload),
  })).json();
}

export async function listPeopleMemory() {
  return (await request(`${API_V1}/memory/people`)).json();
}

export async function confirmMeetingMemory(sessionId, candidates) {
  return (await request(`${API_V1}/meetings/${encodeURIComponent(sessionId)}/memory/confirm`, {
    method: 'POST', body: JSON.stringify({candidates}),
  })).json();
}

export async function getTodayAgenda() { return (await request(`${API_V1}/agenda/today`)).json(); }
export async function createAgendaEvent(payload) { return (await request(`${API_V1}/agenda/events`, {method: 'POST', body: JSON.stringify(payload)})).json(); }
export async function updateAgendaTodo(todoId, done) { return (await request(`${API_V1}/agenda/todos/${encodeURIComponent(todoId)}`, {method: 'PATCH', body: JSON.stringify({done})})).json(); }
export async function listAgendaItems() { return (await request(`${API_V1}/agenda/items`)).json(); }
export async function createAgendaItem(payload) { return (await request(`${API_V1}/agenda/items`, {method: 'POST', body: JSON.stringify(payload)})).json(); }
export async function patchAgendaItem(itemId, payload) { return (await request(`${API_V1}/agenda/items/${encodeURIComponent(itemId)}`, {method: 'PATCH', body: JSON.stringify(payload)})).json(); }
export async function deleteAgendaItem(itemId) { return (await request(`${API_V1}/agenda/items/${encodeURIComponent(itemId)}`, {method: 'DELETE'})).json(); }
export async function bulkDeleteAgendaItems(ids = [], completed = false) { return (await request(`${API_V1}/agenda/items/bulk-delete`, {method: 'POST', body: JSON.stringify({ids, completed})})).json(); }
export async function parseAgendaItem(text, timezone = 'Asia/Shanghai') { return (await request(`${API_V1}/agenda/items/parse`, {method: 'POST', body: JSON.stringify({text, timezone})})).json(); }

export async function getMeetingAudio(sessionId) {
  const response = await request(`${API_V1}/meetings/${encodeURIComponent(sessionId)}/audio`);
  return response.blob();
}

export async function exportMeeting(sessionId, format) {
  const response = await request(`${API_V1}/meetings/${encodeURIComponent(sessionId)}/export?format=${format}`);
  const blob = await response.blob();
  const disposition = response.headers.get('Content-Disposition') || '';
  const encodedName = disposition.match(/filename\*=UTF-8''([^;]+)/i)?.[1];
  const quotedName = disposition.match(/filename="([^"]+)"/i)?.[1];
  let filename = `会议纪要.${format === 'markdown' ? 'md' : format}`;
  try { filename = encodedName ? decodeURIComponent(encodedName) : (quotedName || filename); }
  catch { filename = quotedName || filename; }
  if (window.ClearMeetingAndroid?.saveFile) {
    const base64 = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result).split(',', 2)[1]);
      reader.onerror = reject;
      reader.readAsDataURL(blob);
    });
    window.ClearMeetingAndroid.saveFile(filename, blob.type || 'application/octet-stream', base64);
    return;
  }
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement('a');
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  anchor.remove();
  URL.revokeObjectURL(url);
}
