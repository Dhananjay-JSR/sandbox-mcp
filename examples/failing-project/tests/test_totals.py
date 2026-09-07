"""Written as unittest.TestCase so they run under both pytest and
`python -m unittest`, with or without a network to install anything."""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from billing import apply_discount, invoice_total, line_total, split_tax  # noqa: E402


class LineTotalTests(unittest.TestCase):
    def test_whole_units(self):
        self.assertEqual(line_total(10.0, 3), 30.0)

    def test_zero_quantity(self):
        self.assertEqual(line_total(10.0, 0), 0.0)

    def test_negative_quantity_rejected(self):
        with self.assertRaises(ValueError):
            line_total(10.0, -1)

    def test_fractional_price_is_exact_to_the_cent(self):
        # 0.1 * 3 is 0.30000000000000004 in binary floating point.
        self.assertEqual(line_total(0.10, 3), 0.30)


class DiscountTests(unittest.TestCase):
    def test_no_discount(self):
        self.assertEqual(apply_discount(100.0, 0), 100.0)

    def test_full_discount(self):
        self.assertEqual(apply_discount(100.0, 100), 0.0)

    def test_out_of_range_rejected(self):
        with self.assertRaises(ValueError):
            apply_discount(100.0, 101)

    def test_awkward_percentage_is_exact_to_the_cent(self):
        # 1.15 * 0.85 does not land on 0.9775 in binary floating point.
        self.assertEqual(apply_discount(1.15, 15), 0.98)


class SplitTaxTests(unittest.TestCase):
    def test_single_payer(self):
        self.assertEqual(split_tax(100.0, 0.2, 1), [20.0])

    def test_parts_must_be_positive(self):
        with self.assertRaises(ValueError):
            split_tax(100.0, 0.2, 0)

    def test_split_sums_back_to_the_total(self):
        parts = split_tax(100.0, 0.2, 3)
        self.assertEqual(round(sum(parts), 2), 20.0)


class InvoiceTotalTests(unittest.TestCase):
    def test_simple_invoice(self):
        self.assertEqual(invoice_total([(10.0, 2), (5.0, 4)]), 40.0)

    def test_invoice_with_discount(self):
        self.assertEqual(invoice_total([(10.0, 2), (5.0, 4)], 10), 36.0)

    def test_invoice_with_awkward_values(self):
        # 0.10 * 3 + 0.20 * 3 is 0.9000000000000001 in binary floating point.
        self.assertEqual(invoice_total([(0.10, 3), (0.20, 3)]), 0.90)


if __name__ == "__main__":
    unittest.main()
