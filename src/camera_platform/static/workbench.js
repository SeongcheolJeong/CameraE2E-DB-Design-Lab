'use strict';

// Bounded cache of full, server-verified records. A projection is never a detail.
globalThis.CameraWorkbenchData = function ({api, capacity = 32}) {
  const cache = new Map(), pending = new Map();
  const key = item => item?._projection ? item._projection.collection + ':' + item.id + ':' + item._projection.record_sha256 : null;
  function resolve(item) {
    const id = key(item);
    if (id && ['queued', 'running'].includes(item.status)) {
      // A newer completed detail can precede an older index snapshot. Do not
      // let that snapshot regress the UI or restart polling a finished job.
      let newest = item;
      for (const [cachedKey, record] of cache) {
        if (cachedKey.startsWith(item._projection.collection + ':' + item.id + ':') &&
            record.content_hash === item.content_hash && record.updated_at >= newest.updated_at &&
            (record.updated_at > newest.updated_at || ['succeeded', 'failed', 'cancelled'].includes(record.status))) newest = record;
      }
      if (newest !== item) return newest;
    }
    if (!id || !cache.has(id)) return item;
    const record = cache.get(id); cache.delete(id); cache.set(id, record);
    return record;
  }
  function remember(id, record) {
    cache.delete(id); cache.set(id, record);
    while (cache.size > capacity) cache.delete(cache.keys().next().value);
  }
  async function load(item) {
    const current = resolve(item);
    if (!current?._projection) return current;
    const id = key(item), ref = item._projection;
    if (pending.has(id)) return pending.get(id);
    const path = '/workbench/' + encodeURIComponent(ref.collection) + '/' + encodeURIComponent(item.id);
    if (ref.schema !== 'camera_workbench_projection_v1' || ref.detail_path !== path || !/^[a-f0-9]{64}$/.test(ref.record_sha256)) throw new Error('기록 원본 참조가 올바르지 않습니다.');
    const request = (async () => {
      const detail = await api(path), record = detail.record;
      if (detail.schema !== 'camera_workbench_record_v1' || detail.collection !== ref.collection || record?.id !== item.id || record._projection || record.content_hash !== item.content_hash || !/^[a-f0-9]{64}$/.test(detail.record_sha256 || '')) throw new Error('선택한 기록과 원본 응답이 일치하지 않습니다.');
      if (detail.record_sha256 !== ref.record_sha256) {
        // A worker may finish between the index and detail reads. Immutable
        // documents must match exactly; jobs may only advance in time/state.
        const jobs = ['runs', 'replays', 'factory_jobs', 'surrogates', 'isp_study_cells'];
        const transitions = {queued: ['queued', 'running', 'succeeded', 'failed', 'cancelled'], running: ['running', 'succeeded', 'failed', 'cancelled']};
        if (!jobs.includes(ref.collection) || !transitions[item.status]?.includes(record.status) || !record.updated_at || record.updated_at < item.updated_at) throw new Error('원본 버전이 변경되었거나 실행 상태가 일치하지 않습니다. 새로고침하세요.');
      }
      remember(id, record);
      remember(ref.collection + ':' + item.id + ':' + detail.record_sha256, record);
      return record;
    })();
    pending.set(id, request);
    try { return await request; } finally { pending.delete(id); }
  }
  return {resolve, load};
};
