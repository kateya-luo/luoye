import assert from 'node:assert/strict';
import test from 'node:test';

const storage = new Map();
globalThis.localStorage = {
  getItem: (key) => storage.get(key) || null,
  setItem: (key, value) => storage.set(key, String(value)),
};

const {loadExportPreferences, saveExportPreferences} = await import('./exportPreferences.js');

test('export choices are restored independently for each meeting', () => {
  saveExportPreferences('meeting-a', {
    format: 'markdown', sections: {captions: false, minutes: true, mindmap: false},
    includeInfo: false, watermark: true,
  });
  assert.deepEqual(loadExportPreferences('meeting-a'), {
    format: 'markdown', sections: {captions: false, minutes: true, mindmap: false},
    includeInfo: false, watermark: true,
  });
  assert.equal(loadExportPreferences('meeting-b').format, 'word');
});
