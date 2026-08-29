import assert from 'node:assert/strict';
import test from 'node:test';

import {toWordHtml} from './exportComposer.js';

test('generated Word minutes omit chapter timestamps', () => {
  const html = toWordHtml({
    title: '设备方案评审会',
    created_at: '',
    segments: [],
    summary: {
      summary: '讨论设备方案。',
      decisions: [],
      action_items: [],
      timeline_chapters: [{chapter_no: 1, start_ms: 300000, title: '评审范围', items: ['覆盖通信方案']}],
    },
  }, {
    includeInfo: false,
    sections: {minutes: true, captions: false, mindmap: false},
    watermark: false,
  });

  assert.match(html, /评审范围/);
  assert.doesNotMatch(html, /\b\d{2}:\d{2}\b/);
});
