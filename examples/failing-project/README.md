# invoicing (example project)

A deliberately broken Python project, used to demonstrate
`compare_experiments`.

Three tests fail because the money arithmetic is done in binary floating point:

- `test_fractional_price_is_exact_to_the_cent` — `0.10 * 3 != 0.30`
- `test_awkward_percentage_is_exact_to_the_cent` — `1.15 * 0.85 != 0.98`
- `test_invoice_with_awkward_values` — the same error, compounded

There are three plausible fixes, and they are genuinely not equivalent:

| Approach | Change | Outcome |
|---|---|---|
| A | `round(..., 2)` at each step | fixes some cases, still binary underneath |
| B | `decimal.Decimal` with `ROUND_HALF_UP` | fixes all of them |
| C | `math.floor(x * 100) / 100` | deterministic, but rounds money away |

Run each in its own sandbox, then call `compare_experiments`.

## Running the tests

Works offline, no dependencies:

```
python -m unittest discover -s tests -q
```

Or with pytest, which needs a network to install:

```
setup_commands=["pip install pytest"], network_mode="restricted"
pytest -q
```
