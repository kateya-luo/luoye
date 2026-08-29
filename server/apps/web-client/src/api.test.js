import assert from 'node:assert/strict';
import test from 'node:test';

const storage = new Map();
globalThis.window = {};
globalThis.localStorage = {
  getItem: (key) => storage.get(key) || null,
  setItem: (key, value) => storage.set(key, String(value)),
  removeItem: (key) => storage.delete(key),
};

const api = await import('./api.js');

const jsonResponse = (body, status = 200) => new Response(JSON.stringify(body), {
  status,
  headers: {'Content-Type': 'application/json'},
});

test('login and current-user calls use only the v1 account API', async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/v1/auth/login') return jsonResponse({token: 'account-token'});
    if (url === '/api/v1/me') return jsonResponse({user: {id: 'u-1', username: 'TEST1'}});
    return jsonResponse({}, 404);
  };

  assert.equal(await api.login(' TEST1 ', 'temporary-password'), 'account-token');
  assert.deepEqual(await api.getCurrentUser(), {id: 'u-1', username: 'TEST1'});
  assert.deepEqual(calls.map((call) => call.url), ['/api/v1/auth/login', '/api/v1/me']);
  assert.equal(calls[1].options.headers.get('Authorization'), 'Bearer account-token');
});

test('device management uses the API/2 claim/list/rename/unbind contract', async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/v2/me/devices' && options.method === 'POST') {
      return jsonResponse({device: {device_id: 'LY-001'}});
    }
    if (url === '/api/v2/me/devices') return jsonResponse({devices: [{device_id: 'LY-001'}]});
    if (url === '/api/v2/me/devices/LY-001' && options.method === 'PATCH') {
      return jsonResponse({device: {device_id: 'LY-001', display_name: '会议室'}});
    }
    if (url === '/api/v2/me/devices/LY-001/binding' && options.method === 'DELETE') {
      return jsonResponse({ok: true});
    }
    if (url === '/api/v2/me/devices/claim') return jsonResponse({device: {device_id: 'LY-001'}});
    return jsonResponse({}, 404);
  };

  assert.deepEqual(await api.listDevices(), [{device_id: 'LY-001'}]);
  assert.equal((await api.claimDevice('12 34-56', '会议室')).device_id, 'LY-001');
  assert.equal((await api.renameDevice('LY-001', '会议室')).display_name, '会议室');
  assert.deepEqual(await api.unbindDevice('LY-001'), {ok: true});

  assert.deepEqual(calls.map((call) => call.url), [
    '/api/v2/me/devices',
    '/api/v2/me/devices/claim',
    '/api/v2/me/devices/LY-001',
    '/api/v2/me/devices/LY-001/binding',
  ]);
  assert.deepEqual(JSON.parse(calls[1].options.body), {pairing_code: '123456', display_name: '会议室'});
});

test('structured server errors are shown to the user', async () => {
  globalThis.fetch = async () => jsonResponse({
    error: {code: 'PAIRING_CODE_EXPIRED', message: '配对码已过期'},
  }, 409);

  await assert.rejects(
    () => api.claimDevice('123456'),
    (error) => error.status === 409 && error.code === 'PAIRING_CODE_EXPIRED' && error.message === '配对码已过期',
  );
});

test('fixed SD management stays on the owner-scoped API/2 device routes', async () => {
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({url, options});
    if (options.method === 'POST') return jsonResponse({command_id: 'lysc-1'});
    return jsonResponse({total_bytes: 100, free_bytes: 40, sessions: []});
  };
  assert.equal((await api.getDeviceStorage('LY-001')).free_bytes, 40);
  assert.equal((await api.createDeviceStorageCommand('LY-001', 'delete_sessions', ['s-1'])).command_id, 'lysc-1');
  assert.deepEqual(calls.map((call) => call.url), [
    '/api/v2/me/devices/LY-001/storage',
    '/api/v2/me/devices/LY-001/storage/commands',
  ]);
  assert.deepEqual(JSON.parse(calls[1].options.body), {
    action: 'delete_sessions', session_ids: ['s-1'],
  });
});

test('idempotent GET requests recover from transient gateway errors', async () => {
  const statuses = [502, 503, 200];
  const calls = [];
  globalThis.fetch = async (url, options = {}) => {
    calls.push({url, options});
    const status = statuses.shift();
    return jsonResponse(status === 200 ? {session_id: 'meeting-1'} : {}, status);
  };

  assert.equal((await api.getMeeting('meeting-1')).session_id, 'meeting-1');
  assert.equal(calls.length, 3);
  assert.ok(calls.every((call) => call.url === '/api/v1/meetings/meeting-1'));
});

test('meeting background processing has a dedicated lightweight endpoint', async () => {
  const calls = [];
  globalThis.fetch = async (url) => {
    calls.push(url);
    return jsonResponse({stage: 'transcribing', active: true, progress_percent: 58});
  };
  const status = await api.getMeetingProcessing('meeting / 1');
  assert.equal(status.progress_percent, 58);
  assert.deepEqual(calls, ['/api/v1/meetings/meeting%20%2F%201/processing']);
});

test('mutating requests are not automatically retried', async () => {
  let calls = 0;
  globalThis.fetch = async () => {
    calls += 1;
    return jsonResponse({}, 502);
  };

  await assert.rejects(
    () => api.createMinutesJob('meeting-1', '18'),
    (error) => error.status === 502,
  );
  assert.equal(calls, 1);
});
