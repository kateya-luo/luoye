import assert from 'node:assert/strict';
import test from 'node:test';

import {applyMinutesJobResponse, MINUTES_REFRESH_WARNING} from './minutesJobUi.js';

test('an idempotent ready job is displayed immediately without polling', async () => {
  const calls = [];
  const result = {title: '设备方案评审会', summary: '已完成'};
  const terminal = await applyMinutesJobResponse({id: 'job-1', state: 'ready', result}, {
    setJob: (job) => calls.push(['job', job.id]),
    setShowPicker: (visible) => calls.push(['picker', visible]),
    onMinutesReady: (value) => calls.push(['result', value.title]),
    onChanged: async () => true,
    setError: (message) => calls.push(['error', message]),
  });

  assert.equal(terminal, true);
  assert.deepEqual(calls, [
    ['job', 'job-1'], ['picker', false], ['result', '设备方案评审会'], ['error', ''],
  ]);
});

test('a ready result remains visible when the follow-up refresh fails', async () => {
  const calls = [];
  await applyMinutesJobResponse({id: 'job-2', state: 'ready', result: {title: '保留结果'}}, {
    onMinutesReady: (value) => calls.push(value.title),
    onChanged: async () => false,
    setError: (message) => calls.push(message),
  });
  assert.deepEqual(calls, ['保留结果', MINUTES_REFRESH_WARNING]);
});

test('queued jobs remain non-terminal and continue polling', async () => {
  let stored;
  const terminal = await applyMinutesJobResponse({id: 'job-3', state: 'queued'}, {
    setJob: (job) => { stored = job; },
  });
  assert.equal(terminal, false);
  assert.equal(stored.id, 'job-3');
});
