'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { parseAmount, formatAmount, addAmounts, splitEvenly, minorUnits } = require('../src/money');

test('minorUnits knows the common currencies', () => {
  assert.equal(minorUnits('USD'), 2);
  assert.equal(minorUnits('JPY'), 0);
});

test('minorUnits rejects an unknown currency', () => {
  assert.throws(() => minorUnits('XYZ'), TypeError);
});

test('parseAmount converts to minor units', () => {
  assert.equal(parseAmount('12.34', 'USD'), 1234);
  assert.equal(parseAmount('0.05', 'EUR'), 5);
  assert.equal(parseAmount('7', 'GBP'), 700);
});

test('parseAmount handles zero-decimal currencies', () => {
  assert.equal(parseAmount('1200', 'JPY'), 1200);
});

test('parseAmount handles negatives', () => {
  assert.equal(parseAmount('-3.50', 'USD'), -350);
});

test('parseAmount rejects too many decimal places', () => {
  assert.throws(() => parseAmount('1.234', 'USD'), RangeError);
});

test('parseAmount rejects malformed input', () => {
  assert.throws(() => parseAmount('twelve', 'USD'), TypeError);
  assert.throws(() => parseAmount('', 'USD'), TypeError);
  assert.throws(() => parseAmount(1234, 'USD'), TypeError);
});

test('formatAmount round-trips parseAmount', () => {
  for (const value of ['0.00', '0.01', '9.99', '123.45', '-2.50']) {
    assert.equal(formatAmount(parseAmount(value, 'USD'), 'USD'), value);
  }
});

test('formatAmount pads short amounts', () => {
  assert.equal(formatAmount(5, 'USD'), '0.05');
  assert.equal(formatAmount(0, 'USD'), '0.00');
});

test('addAmounts sums matching currencies', () => {
  const total = addAmounts({ currency: 'USD', minor: 1234 }, { currency: 'USD', minor: 66 });
  assert.deepEqual(total, { currency: 'USD', minor: 1300 });
});

test('addAmounts refuses mismatched currencies', () => {
  assert.throws(
    () => addAmounts({ currency: 'USD', minor: 1 }, { currency: 'EUR', minor: 1 }),
    TypeError,
  );
});

test('splitEvenly distributes the remainder', () => {
  assert.deepEqual(splitEvenly(1000, 3), [334, 333, 333]);
  assert.deepEqual(splitEvenly(10, 4), [3, 3, 2, 2]);
  assert.deepEqual(splitEvenly(9, 3), [3, 3, 3]);
});

test('splitEvenly preserves the total', () => {
  for (const [total, parts] of [[1000, 3], [7, 2], [99, 7]]) {
    const sum = splitEvenly(total, parts).reduce((a, b) => a + b, 0);
    assert.equal(sum, total);
  }
});

test('splitEvenly rejects a bad part count', () => {
  assert.throws(() => splitEvenly(100, 0), RangeError);
  assert.throws(() => splitEvenly(100, 1.5), RangeError);
});
