# payments-service (example project)

A small, dependency-free Node library used to demonstrate Sandbox MCP.

It is pinned to Node 20 and it does not work on Node 22 — for two independent,
entirely realistic reasons:

1. `package.json` declares `engines.node: ">=18 <21"`, and `.npmrc` sets
   `engine-strict=true`. On Node 22, `npm install` fails with `EBADENGINE`
   before a single test runs.
2. `src/crypto.js` calls `crypto.createCipher` / `crypto.createDecipher`.
   Deprecated since Node 10, **removed in Node 22.0.0**. On Node 22 the crypto
   tests fail with `TypeError: crypto.createCipher is not a function`.

The point of the demo is that an agent can discover and fix both of these
inside a disposable sandbox without touching this directory.

Run the tests: `npm test` (that is `node --test test/`).
