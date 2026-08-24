import assert from 'node:assert/strict';
import test from 'node:test';

import {createAudioUploader} from './audioUploader.js';


const tick = () => new Promise((resolve) => setTimeout(resolve, 0));

test('finish queues a separate final request when an audio batch is already in flight', async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  let releaseFirst;
  const firstPending = new Promise((resolve) => { releaseFirst = resolve; });

  globalThis.fetch = async (url, options) => {
    calls.push({url: String(url), options});
    if (calls.length === 1) await firstPending;
    return {ok: true};
  };

  try {
    const uploader = createAudioUploader({sessionId: 'meeting-1', token: 'token'});
    uploader.push(new ArrayBuffer(32000));
    await tick();
    assert.equal(calls.length, 1);
    assert.doesNotMatch(calls[0].url, /final=1/);

    uploader.finish();
    releaseFirst();
    assert.equal(await uploader.flush(1000), true);

    assert.equal(calls.length, 2);
    assert.match(calls[1].url, /start_ms=1000/);
    assert.match(calls[1].url, /final=1/);
    assert.equal(calls[1].options.body.byteLength, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
