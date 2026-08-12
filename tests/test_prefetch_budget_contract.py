# -*- coding: utf-8 -*-
"""未来 PCM 预取的内存预算契约。"""
import unittest

from test_frontend_contracts import PREFETCH_CONTROLLED_SETUP, run_node_contract


class PrefetchBudgetContractTests(unittest.TestCase):
    def test_future_prefetch_over_budget_is_discarded_before_promotion(self):
        """R23: 未来 slot 不能仅因连接数受限而无限累积 Float32 PCM。"""
        assertions = r"""
(async () => {
  await voicesPromise;
  const hasBudget = typeof PREFETCH_MAX_BUFFERED_SAMPLES !== 'undefined'
    && Number.isInteger(PREFETCH_MAX_BUFFERED_SAMPLES)
    && PREFETCH_MAX_BUFFERED_SAMPLES > 0;
  assertOk(
    hasBudget,
    'future prefetch must declare a positive PCM sample budget',
  );
  if (!hasBudget) {
    finish();
    return;
  }

  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r1', startSentence: 1, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const state = must(prefetchMap.get(1), 'missing future prefetch state');
  state.ws.pushPcmSamples(PREFETCH_MAX_BUFFERED_SAMPLES + 1);

  assertOk(state.error && !state.ended, 'over-budget future prefetch becomes an ordered fallback');
  assertOk(state.ws.closed, 'over-budget future socket is closed');
  equal(state.chunks.length, 0, 'uncommitted over-budget PCM is discarded');
  equal(state.sampleCount, 0, 'discarded future state retains no PCM accounting');
  equal(writePos, 0, 'future PCM never reaches the main playback buffer');
  assertOk(prefetchMap.get(1) === state, 'error tombstone remains for ordered fallback');
  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)

    def test_future_prefetch_total_budget_bounds_multiple_slots(self):
        """R23: 两个 future slot 的合计也必须受同一总预算约束。"""
        assertions = r"""
(async () => {
  await voicesPromise;
  const hasBudget = typeof PREFETCH_MAX_BUFFERED_SAMPLES !== 'undefined'
    && Number.isInteger(PREFETCH_MAX_BUFFERED_SAMPLES)
    && PREFETCH_MAX_BUFFERED_SAMPLES > 2;
  assertOk(hasBudget, 'future prefetch must declare a usable total PCM budget');
  if (!hasBudget) {
    finish();
    return;
  }

  runQueue = [
    { engine: 'kokoro', voice: 'af_heart', text: 'r0', startSentence: 0, count: 1 },
    { engine: 'edge', voice: 'zh-CN-XiaoxiaoNeural', text: 'r1', startSentence: 1, count: 1 },
    { engine: 'kokoro', voice: 'af_heart', text: 'r2', startSentence: 2, count: 1 },
  ];
  runIndex = 0;
  writePos = 0;
  streamEnded = false;
  sendRequest();
  await Promise.resolve();
  await Promise.resolve();
  await Promise.resolve();

  const first = must(prefetchMap.get(1), 'missing first future state');
  const second = must(prefetchMap.get(2), 'missing second future state');
  const firstSamples = Math.floor(PREFETCH_MAX_BUFFERED_SAMPLES / 2);
  first.ws.pushPcmSamples(firstSamples);
  assertOk(!first.error, 'first future slot stays valid within the aggregate budget');
  second.ws.pushPcmSamples(PREFETCH_MAX_BUFFERED_SAMPLES - firstSamples + 1);

  assertOk(second.error && !second.ended, 'aggregate overflow becomes an ordered fallback');
  assertOk(second.ws.closed, 'aggregate-overflow socket is closed');
  equal(second.chunks.length, 0, 'aggregate overflow retains no second-slot PCM');
  equal(second.sampleCount, 0, 'aggregate overflow resets second-slot accounting');
  equal(first.sampleCount, firstSamples, 'first-slot PCM is not discarded by another slot overflow');
  equal(writePos, 0, 'future aggregate PCM never reaches the main buffer');
  stop();
  finish();
})().catch(err => { throw err; });
"""
        run_node_contract("index.html", PREFETCH_CONTROLLED_SETUP, assertions)


if __name__ == "__main__":
    unittest.main()
