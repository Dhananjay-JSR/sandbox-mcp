'use strict';

/** Retry with exponential backoff and full jitter. */

function backoffDelay(attempt, { baseMs = 100, maxMs = 5000, jitter = () => 1 } = {}) {
  if (attempt < 1) throw new RangeError('attempt is 1-based');
  const exponential = Math.min(maxMs, baseMs * 2 ** (attempt - 1));
  return Math.round(exponential * jitter());
}

async function withRetry(operation, { attempts = 3, onRetry = () => {}, delay = async () => {} } = {}) {
  let lastError;
  for (let attempt = 1; attempt <= attempts; attempt += 1) {
    try {
      return await operation(attempt);
    } catch (error) {
      lastError = error;
      if (attempt === attempts) break;
      onRetry(error, attempt);
      await delay(backoffDelay(attempt));
    }
  }
  throw lastError;
}

module.exports = { backoffDelay, withRetry };
