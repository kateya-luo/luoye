// 通道 B：浏览器侧可靠音频上传（设计文档 §5 / P1）。
//
// 与实时 WS 并行运行：把采集到的原始 PCM 帧带 seq + 会议时间偏移，
// 通过 HTTP 分片 POST 可靠上传到后端（失败重传、断点续传）。
// 即便实时流断了，只要本地还缓着，最终也会补传上去 → 后端拿到完整音频做离线定稿。
//
// 可靠性保证（断网"一个字不丢"的核心）：
// 1. 断网期间音频只能缓在浏览器（网断了到不了服务器）；本上传器负责"网一恢复就尽快、且绝不丢地补传"。
// 2. 失败=指数退避重试、**永不放弃**（原为固定等2秒，恢复慢；且积压不再被 stop 丢弃）。
// 3. stop() 只停"收新帧"，已入队的继续传完；结束会议前先 await flush() 等积压落库再收尾。
// TODO(P1): 写入 IndexedDB 以防关标签/崩溃丢内存队列（当前仅内存队列，靠 flush 收敛丢失窗口）。

const BYTES_PER_MS = (16000 * 2) / 1000;
const MAX_RETRY_WAIT_MS = 5000;      // 退避上限
const MAX_BATCH_BYTES = 1920 * 1000; // 单次 POST 最多合并 ~60s 音频（断网积压一次性上传）

export function createAudioUploader({sessionId, token, serverBase = ''}) {
  let seq = 0;
  let offsetMs = 0;           // 会议时间偏移，随已入队字节累加
  const queue = [];           // 待上传分片 {seq, startMs, bytes, final?}
  let sending = false;
  let acceptPush = true;      // stop() 后不再接收新帧，但已入队的继续传完（绝不 abandon）
  let finishQueued = false;   // final 必须是独立队列项，避免在途 batch 已复制 final=false 后丢失结束标记
  const drainWaiters = [];    // flush() 的 resolve 列表

  const url = (s) => `${serverBase}/api/v1/sessions/${sessionId}/audio?seq=${s.seq}&start_ms=${s.startMs}${s.final ? '&final=1' : ''}`;

  function settleIfDrained() {
    if (queue.length === 0 && !sending) while (drainWaiters.length) drainWaiters.shift()();
  }

  async function pump() {
    if (sending) return;
    sending = true;
    try {
      while (queue.length) {
        // 合批：把队首起的连续分片并成一个大包一次 POST（分片天然连续：offsetMs 累加）。
        // 高延迟链路上串行小分片追不上实时；合批后断网积压（如 40s）一个请求直接上去。
        const batch = [queue[0]];
        let batchBytes = queue[0].bytes.byteLength;
        for (let i = 1; i < queue.length && batchBytes + queue[i].bytes.byteLength <= MAX_BATCH_BYTES; i++) {
          batch.push(queue[i]);
          batchBytes += queue[i].bytes.byteLength;
        }
        const head = batch[0];
        const item = {seq: head.seq, startMs: head.startMs, final: batch.at(-1).final};
        const body = batch.length === 1 ? head.bytes : new Blob(batch.map((b) => b.bytes));
        for (let attempt = 0; ; attempt++) {
          try {
            const res = await fetch(url(item), {
              method: 'POST',
              headers: {'Content-Type': 'application/octet-stream', ...(token ? {Authorization: `Bearer ${token}`} : {})},
              body,
            });
            if (!res.ok) throw new Error(`HTTP ${res.status}`);
            break;   // 成功
          } catch {
            // 指数退避重试，永不放弃：断网时自然堆积，网一恢复就快速续传，保证一个字不丢
            await new Promise((r) => setTimeout(r, Math.min(MAX_RETRY_WAIT_MS, 300 * 2 ** attempt)));
          }
        }
        queue.splice(0, batch.length);   // 整批 ack 成功才出队
      }
    } finally {
      sending = false;
      settleIfDrained();
    }
  }

  return {
    // 每帧音频入队（与实时 WS sendFrame 并行调用）。buffer: ArrayBuffer(PCM)
    push(buffer) {
      if (!acceptPush) return;
      const startMs = offsetMs;
      offsetMs += Math.round(buffer.byteLength / BYTES_PER_MS);
      queue.push({seq: seq++, startMs, bytes: buffer});
      pump();
    },
    // 会议结束：标记最后一片 final=1，触发后端 finalize 整场重转
    finish() {
      if (finishQueued) return;
      finishQueued = true;
      acceptPush = false;
      // 始终追加独立的零字节结束片。若前一个 batch 已经在 fetch 中，它也不会吞掉 final 标记；
      // FIFO 又保证服务器先收到全部音频，再收到 audio_complete。
      queue.push({seq: seq++, startMs: offsetMs, bytes: new ArrayBuffer(0), final: 1});
      pump();
    },
    // 结束会议时先 await 它：等积压（含断网补传段）全部落到服务器再收尾。
    // 超时则放行——服务器有延迟定稿兜底，晚到的音频仍会补洞。返回是否已排空。
    flush(timeoutMs = 60000) {
      pump();
      if (queue.length === 0 && !sending) return Promise.resolve(true);
      return new Promise((resolve) => {
        let timer;
        const w = () => {
          if (timer) clearTimeout(timer);
          resolve(queue.length === 0);
        };
        drainWaiters.push(w);
        if (timeoutMs) timer = setTimeout(() => {
          const i = drainWaiters.indexOf(w);
          if (i >= 0) drainWaiters.splice(i, 1);
          resolve(queue.length === 0);
        }, timeoutMs);
      });
    },
    // 停止接收新帧，但把已入队的继续传完（不再像原来那样一 stop 就丢弃积压）
    stop() { acceptPush = false; pump(); },
    get backlog() { return queue.length; },
    get offsetMs() { return offsetMs; },   // 当前真实音频偏移（连续累加，不受断网影响）
  };
}
