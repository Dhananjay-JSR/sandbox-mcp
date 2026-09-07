'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { isValidCardNumber, normalizeReference, isExpired, luhnChecksum } = require('../src/validate');

test('luhnChecksum is zero for a valid number', () => {
  assert.equal(luhnChecksum('4242424242424242'), 0);
});

test('luhnChecksum rejects non-digits', () => {
  assert.throws(() => luhnChecksum('4242-4242'), TypeError);
});

test('isValidCardNumber accepts known-good test numbers', () => {
  for (const number of ['4242424242424242', '5555555555554444', '378282246310005']) {
    assert.equal(isValidCardNumber(number), true, number);
  }
});

test('isValidCardNumber tolerates spaces and dashes', () => {
  assert.equal(isValidCardNumber('4242 4242 4242 4242'), true);
  assert.equal(isValidCardNumber('4242-4242-4242-4242'), true);
});

test('isValidCardNumber rejects a bad checksum', () => {
  assert.equal(isValidCardNumber('4242424242424243'), false);
});

test('isValidCardNumber rejects wrong lengths', () => {
  assert.equal(isValidCardNumber('42424242'), false);
  assert.equal(isValidCardNumber('4'.repeat(20)), false);
});

test('isValidCardNumber rejects non-strings', () => {
  assert.equal(isValidCardNumber(4242424242424242), false);
  assert.equal(isValidCardNumber(null), false);
});

test('normalizeReference strips punctuation and upcases', () => {
  assert.equal(normalizeReference('  inv-2024/07 #12 '), 'INV20240712');
});

test('normalizeReference rejects empty results', () => {
  assert.throws(() => normalizeReference('---'), RangeError);
  assert.throws(() => normalizeReference(42), TypeError);
});

test('isExpired compares against end of month', () => {
  const now = new Date(Date.UTC(2024, 5, 15));
  assert.equal(isExpired('05/24', now), true);
  assert.equal(isExpired('06/24', now), false);
  assert.equal(isExpired('12/30', now), false);
});

test('isExpired rejects malformed expiry', () => {
  assert.throws(() => isExpired('13/24'), TypeError);
  assert.throws(() => isExpired('2024-06'), TypeError);
});
