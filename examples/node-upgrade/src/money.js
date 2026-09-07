'use strict';

/** Money is stored in minor units (cents) to keep arithmetic exact. */

const CURRENCIES = { USD: 2, EUR: 2, GBP: 2, JPY: 0 };

function minorUnits(currency) {
  const exponent = CURRENCIES[currency];
  if (exponent === undefined) throw new TypeError(`Unsupported currency: ${currency}`);
  return exponent;
}

function parseAmount(value, currency) {
  if (typeof value !== 'string' || value.trim() === '') {
    throw new TypeError('Amount must be a non-empty string');
  }
  if (!/^-?\d+(\.\d+)?$/.test(value.trim())) {
    throw new TypeError(`Malformed amount: ${value}`);
  }
  const exponent = minorUnits(currency);
  const [whole, fraction = ''] = value.trim().split('.');
  if (fraction.length > exponent) {
    throw new RangeError(`${currency} allows at most ${exponent} decimal places`);
  }
  const padded = fraction.padEnd(exponent, '0');
  const sign = whole.startsWith('-') ? -1 : 1;
  const magnitude = Number(whole.replace('-', '') + padded);
  return sign * magnitude;
}

function formatAmount(minor, currency) {
  const exponent = minorUnits(currency);
  const sign = minor < 0 ? '-' : '';
  const digits = String(Math.abs(minor)).padStart(exponent + 1, '0');
  if (exponent === 0) return `${sign}${digits}`;
  return `${sign}${digits.slice(0, -exponent)}.${digits.slice(-exponent)}`;
}

function addAmounts(a, b) {
  if (a.currency !== b.currency) {
    throw new TypeError(`Cannot add ${a.currency} to ${b.currency}`);
  }
  return { currency: a.currency, minor: a.minor + b.minor };
}

function splitEvenly(minor, parts) {
  if (!Number.isInteger(parts) || parts < 1) throw new RangeError('parts must be >= 1');
  const base = Math.trunc(minor / parts);
  const remainder = minor - base * parts;
  return Array.from({ length: parts }, (_, index) => base + (index < remainder ? 1 : 0));
}

module.exports = { parseAmount, formatAmount, addAmounts, splitEvenly, minorUnits };
