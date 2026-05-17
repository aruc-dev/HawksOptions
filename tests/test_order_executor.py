from __future__ import annotations

import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.execution_quality import execution_quality_summary
from core.limit_price import limit_price_improvement_plan
from core.models import OptionContract, OrderLeg, StrategyOrder
from core.order_executor import build_order_payload, execute_order, persist_open_order, trade_log_rows_from_order
from core.trade_log import read_trade_rows


def _order() -> StrategyOrder:
    short = OptionContract(
        contract_symbol="SPY260619P00500000",
        underlying="SPY",
        option_type="put",
        strike=500.0,
        expiration=date(2026, 6, 19),
        bid=1.0,
        ask=1.1,
        open_interest=500,
        volume=20,
        implied_volatility=0.2,
        delta=-0.2,
        theta=-0.1,
        vega=0.2,
        gamma=0.01,
        underlying_price=520.0,
    )
    long = OptionContract(
        contract_symbol="SPY260619P00499000",
        underlying="SPY",
        option_type="put",
        strike=499.0,
        expiration=date(2026, 6, 19),
        bid=0.8,
        ask=0.9,
        open_interest=500,
        volume=20,
        implied_volatility=0.2,
        delta=-0.18,
        theta=-0.1,
        vega=0.2,
        gamma=0.01,
        underlying_price=520.0,
    )
    return StrategyOrder(
        strategy_name="vertical_spread",
        strategy_id="vertical_spread-SPY-20260423",
        underlying="SPY",
        legs=[OrderLeg(contract=short, side="sell_to_open"), OrderLeg(contract=long, side="buy_to_open")],
        max_loss=80.0,
        max_profit=20.0,
        required_buying_power=80.0,
        profit_take_pct=0.5,
        loss_stop_multiple=1.5,
        roll_threshold_delta=-0.4,
        iv_rank=50.0,
        required_options_level=3,
    )


class _QuoteClient:
    def get_option_quotes(self, symbols):
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {
            "SPY260619P00500000": {"bid": 1.2, "ask": 1.4, "source": "unit", "timestamp": timestamp},
            "SPY260619P00499000": {"bid": 0.7, "ask": 0.8, "source": "unit", "timestamp": timestamp},
        }


class _LiveQuoteFallbackClient:
    def __init__(self):
        self.payloads = []

    def get_option_quotes(self, symbols):
        raise NotImplementedError("live option quote retrieval is not implemented")

    def submit_order(self, payload):
        self.payloads.append(payload)
        return {"id": "live-1", "status": "accepted", "payload": payload}


class _LiveQuoteSubmitClient(_QuoteClient):
    def __init__(self):
        self.payloads = []

    def submit_order(self, payload):
        self.payloads.append(payload)
        return {"id": "live-1", "status": "accepted", "payload": payload}


class _InvalidQuoteSubmitClient(_LiveQuoteSubmitClient):
    def get_option_quotes(self, symbols):
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {
            "SPY260619P00500000": {"bid": 0.0, "ask": 0.0, "source": "unit", "timestamp": timestamp},
            "SPY260619P00499000": {"bid": 0.7, "ask": 0.8, "source": "unit", "timestamp": timestamp},
        }


class _StaleQuoteSubmitClient(_LiveQuoteSubmitClient):
    def get_option_quotes(self, symbols):
        timestamp = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
        return {
            "SPY260619P00500000": {"bid": 1.2, "ask": 1.4, "source": "unit", "timestamp": timestamp},
            "SPY260619P00499000": {"bid": 0.7, "ask": 0.8, "source": "unit", "timestamp": timestamp},
        }


class OrderExecutorTests(unittest.TestCase):
    def test_build_multileg_payload(self):
        payload = build_order_payload(_order())
        self.assertEqual(payload["order_class"], "mleg")
        self.assertEqual(payload["limit_price"], 0.2)
        self.assertEqual(len(payload["legs"]), 2)

    def test_limit_price_plan_respects_min_credit_quality(self):
        order = _order()
        config = {
            "strategies": {
                "vertical_spread": {
                    "min_net_credit": 15.0,
                    "min_credit_to_width": 0.10,
                }
            }
        }

        plan = limit_price_improvement_plan(order, config=config)

        self.assertEqual(plan["price_type"], "credit")
        self.assertEqual(plan["initial_net_credit"], 20.0)
        self.assertEqual(plan["max_acceptable_net_credit"], 15.0)
        self.assertEqual(plan["guardrail"], "min_credit")
        self.assertEqual(plan["schedule"][0]["limit_price"], 0.2)
        self.assertEqual(plan["schedule"][-1]["limit_price"], 0.15)

    def test_debit_limit_price_plan_does_not_exceed_max_loss(self):
        order = StrategyOrder(
            strategy_name="calendar_spread",
            strategy_id="calendar-SPY-20260423",
            underlying="SPY",
            legs=[
                OrderLeg(
                    contract=OptionContract(
                        "SPY260619C00520000",
                        "SPY",
                        "call",
                        520.0,
                        date(2026, 6, 19),
                        bid=1.0,
                        ask=1.1,
                        underlying_price=520.0,
                    ),
                    side="buy_to_open",
                )
            ],
            max_loss=110.0,
            max_profit=165.0,
            required_buying_power=110.0,
            profit_take_pct=0.5,
            loss_stop_multiple=1.0,
            roll_threshold_delta=None,
            iv_rank=20.0,
        )
        order.metadata["limit_price_improvement_settings"] = {
            "steps": 2,
            "max_debit_pct_of_max_loss": 0.95,
        }

        plan = limit_price_improvement_plan(order)

        self.assertEqual(plan["price_type"], "debit")
        self.assertEqual(plan["initial_limit_price"], 1.05)
        self.assertEqual(plan["max_acceptable_debit"], 105.0)
        self.assertEqual(plan["max_acceptable_net_credit"], -105.0)
        self.assertEqual(plan["guardrail"], "max_debit")

    def test_dry_run_execution_quality_uses_expected_fills(self):
        order = _order()
        result = execute_order(object(), order, dry_run=True)

        quality = result["execution_quality"]

        self.assertEqual(result["payload"]["limit_price"], 0.2)
        self.assertIn("limit_price_improvement", result)
        self.assertIn("limit_price_improvement", order.metadata)
        self.assertEqual(quality["expected_net_opening_credit"], order.net_opening_credit)
        self.assertEqual(quality["actual_net_opening_credit"], order.net_opening_credit)
        self.assertFalse(quality["partial_fill"])
        self.assertEqual(quality["retry_count"], 0)
        self.assertEqual(len(quality["legs"]), 2)
        self.assertIn("execution_quality", order.metadata)

    def test_execution_quality_uses_nbbo_snapshot_midpoints(self):
        order = _order()
        result = execute_order(_QuoteClient(), order, dry_run=True)
        quality = result["execution_quality"]

        self.assertIn("nbbo_snapshot", result)
        self.assertEqual(quality["legs"][0]["expected_price"], 1.3)
        self.assertEqual(quality["legs"][1]["expected_price"], 0.75)
        self.assertEqual(quality["expected_net_opening_credit"], 55.0)

    def test_live_execution_requires_fresh_nbbo_quotes(self):
        order = _order()
        client = _LiveQuoteFallbackClient()

        with self.assertRaisesRegex(RuntimeError, "fresh_nbbo_required_for_live_order"):
            execute_order(client, order, dry_run=False)

        self.assertEqual(client.payloads, [])

    def test_live_execution_submits_with_fresh_nbbo_quotes(self):
        order = _order()
        client = _LiveQuoteSubmitClient()

        result = execute_order(client, order, dry_run=False)

        self.assertEqual(result["id"], "live-1")
        self.assertEqual(result["nbbo_snapshot"]["legs"][0]["source"], "unit")
        self.assertEqual(result["execution_quality"]["expected_net_opening_credit"], 55.0)
        self.assertEqual(len(client.payloads), 1)

    def test_live_execution_rejects_invalid_client_nbbo_quotes(self):
        order = _order()
        client = _InvalidQuoteSubmitClient()

        with self.assertRaisesRegex(RuntimeError, "fresh_nbbo_required_for_live_order"):
            execute_order(client, order, dry_run=False)

        self.assertEqual(client.payloads, [])
        self.assertEqual(order.metadata["nbbo_snapshot"]["legs"][0]["source"], "order_contract")

    def test_live_execution_rejects_stale_client_nbbo_quotes(self):
        order = _order()
        client = _StaleQuoteSubmitClient()

        with self.assertRaisesRegex(RuntimeError, "fresh_nbbo_required_for_live_order"):
            execute_order(client, order, dry_run=False)

        self.assertEqual(client.payloads, [])

    def test_trade_log_uses_nbbo_expected_prices(self):
        order = _order()
        result = execute_order(_QuoteClient(), order, dry_run=True)
        rows = trade_log_rows_from_order(order, mode="paper", order_id="dryrun", execution_result=result)

        self.assertEqual(rows[0]["expected_entry_price"], 1.3)
        self.assertEqual(rows[1]["expected_entry_price"], 0.75)

    def test_execution_quality_tracks_partial_leg_slippage(self):
        order = _order()
        response = {
            "status": "partially_filled",
            "retry_count": 2,
            "submitted_at": "2026-04-23T10:00:00+00:00",
            "filled_at": "2026-04-23T10:00:07+00:00",
            "legs": [
                {"symbol": "SPY260619P00500000", "filled_avg_price": 1.00, "filled_qty": 1},
                {"symbol": "SPY260619P00499000", "filled_avg_price": 0.92, "filled_qty": 0},
            ],
        }

        quality = execution_quality_summary(order, response=response)

        self.assertTrue(quality["partial_fill"])
        self.assertEqual(quality["retry_count"], 2)
        self.assertEqual(quality["order_duration_seconds"], 7.0)
        self.assertEqual(quality["legs"][0]["slippage_dollars"], 5.0)
        self.assertEqual(quality["legs"][1]["slippage_dollars"], 0.0)
        self.assertIsNone(quality["actual_net_opening_credit"])
        self.assertIsNone(quality["net_slippage_dollars"])

    def test_execution_quality_order_level_net_requires_all_legs_filled(self):
        order = _order()
        response = {
            "legs": [
                {"symbol": "SPY260619P00500000", "filled_avg_price": 1.00, "filled_qty": 1},
            ],
        }

        quality = execution_quality_summary(order, response=response)

        self.assertIsNone(quality["actual_net_opening_credit"])
        self.assertIsNone(quality["net_slippage_dollars"])
        self.assertEqual(quality["legs"][0]["actual_cashflow"], 100.0)
        self.assertIsNone(quality["legs"][1]["actual_cashflow"])

    def test_persist_open_order_writes_trade_log_and_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            trade_log = Path(tmp) / "trades.csv"
            positions = Path(tmp) / "positions.json"
            order = _order()
            execution_result = execute_order(object(), order, dry_run=True)
            snapshot = persist_open_order(
                order=order,
                mode="paper",
                order_id="ord-1",
                trade_log_path=trade_log,
                positions_path=positions,
                execution_result=execution_result,
            )
            self.assertTrue(trade_log.exists())
            self.assertTrue(positions.exists())
            self.assertEqual(snapshot.strategy_id, "vertical_spread-SPY-20260423")
            rows = read_trade_rows(trade_log)
            self.assertEqual(rows[0]["expected_entry_price"], "1.05")
            self.assertEqual(rows[0]["actual_entry_price"], "1.05")
            self.assertEqual(rows[0]["partial_fill"], "False")
            self.assertEqual(rows[0]["retry_count"], "0")

    def test_trade_log_marks_partial_fills(self):
        with tempfile.TemporaryDirectory() as tmp:
            trade_log = Path(tmp) / "trades.csv"
            positions = Path(tmp) / "positions.json"
            order = _order()
            execution_result = {
                "execution_quality": execution_quality_summary(
                    order,
                    response={
                        "status": "partially_filled",
                        "legs": [
                            {"symbol": "SPY260619P00500000", "filled_avg_price": 1.00, "filled_qty": 1},
                            {"symbol": "SPY260619P00499000", "filled_avg_price": 0.90, "filled_qty": 0},
                        ],
                    },
                )
            }

            persist_open_order(
                order=order,
                mode="paper",
                order_id="ord-2",
                trade_log_path=trade_log,
                positions_path=positions,
                execution_result=execution_result,
            )

            rows = read_trade_rows(trade_log)
            self.assertEqual(rows[0]["status"], "partially_filled")
            self.assertEqual(rows[0]["partial_fill"], "True")

    def test_trade_log_rows_do_not_emit_none_for_unknown_fills(self):
        rows = trade_log_rows_from_order(
            _order(),
            mode="paper",
            order_id="ord-unknown",
            execution_result={"execution_quality": {"legs": [{"leg_number": 1, "actual_price": None, "slippage_dollars": None}]}},
        )

        self.assertEqual(rows[0]["actual_entry_price"], "")
        self.assertEqual(rows[0]["leg_slippage_dollars"], "")
        self.assertEqual(rows[0]["order_duration_seconds"], "")
        self.assertNotIn(None, rows[0].values())


if __name__ == "__main__":
    unittest.main()
