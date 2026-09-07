'use strict';

/** Card and reference validation. No network, no dependencies. */

function luhnChecksum(digits) {
  let sum = 0;
  let double = false;
  for (let index = digits.length - 1; index >= 0; index -= 1) {
    let value = digits.charCodeAt(index) - 48;
    if (value < 0 || value > 9) throw new TypeError('Card number must be digits only');
    if (double) {
      value *= 2;
      if (value > 9) value -= 9;
    }
    sum += value;
    double = !double;
  }
  return sum % 10;
}

function isValidCardNumber(input) {
  if (typeof input !== 'string') return false;
  const digits = input.replace(/[\s-]/g, '');
  if (digits.length < 12 || digits.length > 19) return false;
  try {
    return luhnChecksum(digits) === 0;
  } catch {
    return false;
  }
}

function normalizeReference(reference) {
  if (typeof reference !== 'string') throw new TypeError('Reference must be a string');
  const trimmed = reference.trim().toUpperCase().replace(/[^A-Z0-9]/g, '');
  if (trimmed.length === 0) throw new RangeError('Reference is empty after normalisation');
  return trimmed;
}

function isExpired(expiry, now = new Date()) {
  const match = /^(0[1-9]|1[0-2])\/(\d{2})$/.exec(expiry);
  if (!match) throw new TypeError(`Malformed expiry: ${expiry}`);
  const month = Number(match[1]);
  const year = 2000 + Number(match[2]);
  const endOfMonth = new Date(Date.UTC(year, month, 1) - 1);
  return endOfMonth.getTime() < now.getTime();
}

module.exports = { isValidCardNumber, normalizeReference, isExpired, luhnChecksum };
