'use strict';

const crypto = require('crypto');

/**
 * Reversible sealing for payment references we hand to third parties.
 *
 * NOTE: this uses crypto.createCipher, which derives its key from the password
 * with MD5 and uses no IV. It has been deprecated since Node 10 (DEP0106) and
 * nobody has got round to migrating it.
 */

const ALGORITHM = 'aes-192-cbc';

function seal(plaintext, password) {
  if (typeof plaintext !== 'string') throw new TypeError('plaintext must be a string');
  if (typeof password !== 'string' || password.length < 8) {
    throw new TypeError('password must be a string of at least 8 characters');
  }
  const cipher = crypto.createCipher(ALGORITHM, password);
  return cipher.update(plaintext, 'utf8', 'hex') + cipher.final('hex');
}

function open(sealed, password) {
  if (typeof sealed !== 'string') throw new TypeError('sealed must be a string');
  const decipher = crypto.createDecipher(ALGORITHM, password);
  return decipher.update(sealed, 'hex', 'utf8') + decipher.final('utf8');
}

/** Non-reversible fingerprint, used for idempotency keys. */
function fingerprint(value) {
  return crypto.createHash('sha256').update(String(value)).digest('hex').slice(0, 32);
}

module.exports = { seal, open, fingerprint, ALGORITHM };
