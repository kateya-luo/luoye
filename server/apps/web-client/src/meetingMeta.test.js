import assert from 'node:assert/strict';
import test from 'node:test';

import {meetingExportBaseName, safeFileBaseName} from './meetingMeta.js';

test('meeting exports use the meeting title rather than the session id', () => {
  assert.equal(meetingExportBaseName({title: '设备方案评审会', session_id: 'ly-secret-id'}), '设备方案评审会');
});

test('meeting export names are safe on Windows and retain readable Chinese', () => {
  assert.equal(safeFileBaseName('  评审：4G/LoRa？  '), '评审-4G-LoRa-');
  assert.equal(safeFileBaseName('CON'), 'CON-会议');
  assert.equal(safeFileBaseName('...'), '未命名会议');
});
