'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { backoffDelay, withRetry } = require('../src/retry');

test('backoffDelay grows exponentially', () => {
  assert.equal(backoffDelay(1), 100);
  assert.equal(backoffDelay(2), 200);
  assert.equal(backoffDelay(3), 400);
});

test('backoffDelay respects the ceiling', () => {
  assert.equal(backoffDelay(20, { maxMs: 5000 }), 5000);
});

test('backoffDelay applies jitter', () => {
  assert.equal(backoffDelay(3, { jitter: () => 0.5 }), 200);
});

test('backoffDelay rejects a zero attempt', () => {
  assert.throws(() => backoffDelay(0), RangeError);
});

test('withRetry returns the first success', async () => {
  let calls = 0;
  const result = await withRetry(async () => {
    calls += 1;
    return 'ok';
  });
  assert.equal(result, 'ok');
  assert.equal(calls, 1);
});

test('withRetry retries until it succeeds', async () => {
  let calls = 0;
  const result = await withRetry(
    async (attempt) => {
      calls += 1;
      if (attempt < 3) throw new Error('flaky');
      return attempt;
    },
    { attempts: 5 },
  );
  assert.equal(result, 3);
  assert.equal(calls, 3);
});

test('withRetry rethrows the last error', async () => {
  await assert.rejects(
    withRetry(async () => {
      throw new Error('always down');
    }, { attempts: 2 }),
    /always down/,
  );
});

test('withRetry reports each retry', async () => {
  const seen = [];
  await assert.rejects(
    withRetry(async () => { throw new Error('nope'); }, {
      attempts: 3,
      onRetry: (_error, attempt) => seen.push(attempt),
    }),
  );
  assert.deepEqual(seen, [1, 2]);
});
