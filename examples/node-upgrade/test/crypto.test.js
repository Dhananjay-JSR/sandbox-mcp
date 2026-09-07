'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { seal, open, fingerprint } = require('../src/crypto');

const PASSWORD = 'a-long-enough-password';

test('seal produces hex output', () => {
  const sealed = seal('order-1234', PASSWORD);
  assert.match(sealed, /^[0-9a-f]+$/);
});

test('seal and open round-trip', () => {
  assert.equal(open(seal('order-1234', PASSWORD), PASSWORD), 'order-1234');
});

test('seal round-trips unicode', () => {
  const value = 'facture-café-€42';
  assert.equal(open(seal(value, PASSWORD), PASSWORD), value);
});

test('open with the wrong password fails', () => {
  assert.throws(() => open(seal('secret', PASSWORD), 'another-password-x'));
});

test('seal rejects bad input', () => {
  assert.throws(() => seal(42, PASSWORD), TypeError);
  assert.throws(() => seal('x', 'short'), TypeError);
});

test('fingerprint is stable and truncated', () => {
  assert.equal(fingerprint('order-1234').length, 32);
  assert.equal(fingerprint('order-1234'), fingerprint('order-1234'));
});

test('fingerprint separates different inputs', () => {
  assert.notEqual(fingerprint('a'), fingerprint('b'));
});
