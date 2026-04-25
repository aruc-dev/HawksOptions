from __future__ import annotations

import unittest

from core.greeks_calculator import black_scholes_greeks, black_scholes_price


class BlackScholesTests(unittest.TestCase):
    def test_textbook_call_price(self):
        price = black_scholes_price("call", 100, 100, 1.0, 0.05, 0.20)
        self.assertAlmostEqual(price, 10.4506, places=3)

    def test_textbook_put_price(self):
        price = black_scholes_price("put", 100, 100, 1.0, 0.05, 0.20)
        self.assertAlmostEqual(price, 5.5735, places=3)

    def test_greeks_have_expected_signs(self):
        greeks = black_scholes_greeks("call", 100, 100, 1.0, 0.05, 0.20)
        self.assertGreater(greeks.delta, 0)
        self.assertGreater(greeks.gamma, 0)
        self.assertGreater(greeks.vega, 0)
        self.assertLess(greeks.theta, 0)


if __name__ == "__main__":
    unittest.main()
