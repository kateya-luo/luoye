import assert from 'node:assert/strict';
import test from 'node:test';
import {formatProcessingEta, processingStatusLabel} from './processingStatus.js';

test('processing ETA is rendered as a readable range', () => {
  assert.equal(formatProcessingEta({eta_lower_seconds: 70, eta_upper_seconds: 190}), '预计约 1–4 分钟');
  assert.equal(formatProcessingEta({eta_lower_seconds: 10, eta_upper_seconds: 45}), '预计不到 1 分钟');
});

test('queue position is explicit instead of silently extending the estimate', () => {
  assert.equal(
    formatProcessingEta({eta_lower_seconds: 60, eta_upper_seconds: 120, queue_ahead_jobs: 3}),
    '预计约 1–2 分钟（前方还有 3 个任务，实际等待可能增加）',
  );
});

test('upload and processing states have distinct labels', () => {
  assert.equal(formatProcessingEta({stage: 'uploading'}), '上传耗时取决于当前网络，传完后会自动继续');
  assert.equal(processingStatusLabel({stage: 'uploading', active: true}), '上传中');
  assert.equal(processingStatusLabel({stage: 'transcribing', active: true}), '后台处理中');
  assert.equal(processingStatusLabel({stage: 'failed', active: false}), '处理异常');
  assert.equal(processingStatusLabel({stage: 'stalled', active: false}), '处理异常');
});
