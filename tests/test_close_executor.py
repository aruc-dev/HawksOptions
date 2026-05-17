from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from core.close_executor import build_close_order_payload, close_order_plans
from core.models import OptionContract, OrderLeg, PositionSnapshot
from core.trade_log import TRADE_LOG_FIELDS, append_trade_rows, mark_strategy_closed, read_trade_rows


def _position(*, qty: int = 1) -> PositionSnapshot:
    short = OptionContract(
        "SPY260619P00500000",
        "SPY",
        "put",
        500.0,
        date(2026, 6, 19),
        1.0,
        1.1,
        underlying_price=520.0,
    )
    long = OptionContract(
        "SPY260619P00499000",
        "SPY",
        "put",
        499.0,
        date(2026, 6, 19),
        0.8,
        0.9,
        underlying_price=520.0,
    )
    return PositionSnapshot(
        strategy_id="vertical_spread-SPY-20260423",
        strategy_name="vertical_spread",
        underlying="SPY",
        legs=[
            OrderLeg(short, "sell_to_open", qty=qty),
            OrderLeg(long, "buy_to_open", qty=qty),
        ],
        opened_at=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
        entry_credit=20.0,
        max_loss=80.0,
        profit_take_pct=0.5,
        loss_stop_multiple=1.5,
        roll_threshold_delta=-0.4,
    )


def _butterfly_position() -> PositionSnapshot:
    low = OptionContract(
        "SPY260619P00495000",
        "SPY",
        "put",
        495.0,
        date(2026, 6, 19),
        0.15,
        0.25,
        underlying_price=520.0,
    )
    middle = OptionContract(
        "SPY260619P00500000",
        "SPY",
        "put",
        500.0,
        date(2026, 6, 19),
        0.45,
        0.55,
        underlying_price=520.0,
    )
    high = OptionContract(
        "SPY260619P00505000",
        "SPY",
        "put",
        505.0,
        date(2026, 6, 19),
        0.95,
        1.05,
        underlying_price=520.0,
    )
    return PositionSnapshot(
        strategy_id="butterfly-SPY-20260423",
        strategy_name="butterfly",
        underlying="SPY",
        legs=[
            OrderLeg(low, "buy_to_open", qty=1),
            OrderLeg(middle, "sell_to_open", qty=2),
            OrderLeg(high, "buy_to_open", qty=1),
        ],
        opened_at=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
        entry_credit=-20.0,
        max_loss=20.0,
        profit_take_pct=0.5,
        loss_stop_multiple=1.5,
        roll_threshold_delta=None,
    )


class _Client:
    def __init__(self, *, submit_status: str = "accepted"):
        self.payloads = []
        self.statuses = {}
        self.submit_status = submit_status

    def submit_order(self, payload):
        self.payloads.append(payload)
        return {"id": "close-1", "status": self.submit_status}

    def get_order_status(self, order_id):
        status = self.statuses.get(order_id, "")
        if isinstance(status, Exception):
            raise status
        return status

    def get_option_quotes(self, symbols):
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {
            symbol: {"bid": 1.0, "ask": 1.1, "source": "test_quote", "timestamp": timestamp}
            for symbol in symbols
        }


class _NoStatusClient(_Client):
    get_order_status = None


class _InvalidQuoteClient(_Client):
    def get_option_quotes(self, symbols):
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return {
            symbol: {"bid": 0.0, "ask": 0.0, "source": "bad_quote", "timestamp": timestamp}
            for symbol in symbols
        }


class CloseExecutorTests(unittest.TestCase):
    def test_build_close_order_payload_uses_broker_close_sides(self):
        payload = build_close_order_payload(_position())

        self.assertEqual(payload["position_intent"], "close")
        self.assertEqual(payload["order_class"], "mleg")
        self.assertEqual(payload["legs"][0]["side"], "buy")
        self.assertEqual(payload["legs"][1]["side"], "sell")
        self.assertNotIn("qty", payload["legs"][0])
        self.assertNotIn("qty", payload["legs"][1])
        self.assertEqual(payload["limit_price"], 0.2)

    def test_build_close_order_payload_keeps_qty_for_single_leg_close(self):
        position = _position()
        position.legs = position.legs[:1]
        payload = build_close_order_payload(position)

        self.assertEqual(payload["symbol"], "SPY260619P00500000")
        self.assertEqual(payload["qty"], 1)
        self.assertEqual(payload["ratio_qty"], 1)

    def test_close_order_plans_do_not_submit_when_disabled(self):
        client = _Client()
        plans = close_order_plans(
            [_position()],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "take_profit"}],
            client=client,
            execute_enabled=False,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["status"], "planned")
        self.assertEqual(client.payloads, [])

    def test_close_order_plans_submit_only_when_enabled_and_not_dry_run(self):
        client = _Client()
        position = _position()
        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "take_profit"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["id"], "close-1")
        self.assertEqual(len(client.payloads), 1)
        self.assertEqual(position.pending_close_order_id, "close-1")
        self.assertEqual(position.pending_close_action, "take_profit")
        self.assertIn("nbbo_snapshot", plans[0])

    def test_close_order_plans_requires_fresh_nbbo_for_live_close(self):
        client = _InvalidQuoteClient()
        with self.assertRaisesRegex(RuntimeError, "fresh_nbbo_required_for_live_close"):
            close_order_plans(
                [_position()],
                [{"strategy_id": "vertical_spread-SPY-20260423", "action": "take_profit"}],
                client=client,
                execute_enabled=True,
                dry_run=False,
            )
        self.assertEqual(client.payloads, [])

    def test_build_close_order_payload_normalizes_limit_by_position_quantity(self):
        payload = build_close_order_payload(_position(qty=3))

        self.assertEqual(payload["limit_price"], 0.2)

    def test_build_close_order_payload_uses_gcd_for_ratio_spread_unit(self):
        payload = build_close_order_payload(_butterfly_position())

        self.assertEqual(payload["limit_price"], 0.2)
        self.assertEqual([leg["ratio_qty"] for leg in payload["legs"]], [1, 2, 1])

    def test_close_order_plans_tracks_immediate_partial_fill_as_pending(self):
        client = _Client(submit_status="partially_filled")
        position = _position()
        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "take_profit"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["status"], "partially_filled")
        self.assertEqual(position.pending_close_order_id, "close-1")
        self.assertEqual(position.pending_close_action, "take_profit")

    def test_close_order_plans_tracks_active_broker_status_as_pending(self):
        client = _Client(submit_status="accepted_for_bidding")
        position = _position()
        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "take_profit"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["status"], "accepted_for_bidding")
        self.assertEqual(position.pending_close_order_id, "close-1")
        self.assertEqual(position.pending_close_action, "take_profit")

    def test_close_order_plans_dedupe_duplicate_actions_by_priority(self):
        client = _Client()
        plans = close_order_plans(
            [_position()],
            [
                {"strategy_id": "vertical_spread-SPY-20260423", "action": "time_exit"},
                {"strategy_id": "vertical_spread-SPY-20260423", "action": "stop_loss"},
            ],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(len(plans), 1)
        self.assertEqual(plans[0]["action"], "stop_loss")
        self.assertEqual(len(client.payloads), 1)

    def test_close_order_plans_skip_positions_already_pending_close(self):
        client = _Client()
        position = _position()
        position.pending_close_order_id = "close-pending"
        position.pending_close_submitted_at = datetime.now(timezone.utc)
        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "take_profit"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["status"], "skipped_pending_close")
        self.assertEqual(client.payloads, [])

    def test_close_order_plans_clear_terminal_pending_close_status(self):
        client = _Client()
        client.statuses["close-pending"] = "canceled"
        position = _position()
        position.pending_close_order_id = "close-pending"
        position.pending_close_action = "take_profit"
        position.pending_close_submitted_at = datetime.now(timezone.utc)

        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "stop_loss"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["id"], "close-1")
        self.assertEqual(position.pending_close_order_id, "close-1")
        self.assertEqual(position.pending_close_action, "stop_loss")

    def test_close_order_plans_clear_stale_unknown_pending_close(self):
        client = _Client()
        position = _position()
        position.pending_close_order_id = "close-stale"
        position.pending_close_action = "take_profit"
        position.pending_close_submitted_at = datetime.now(timezone.utc) - timedelta(minutes=30)

        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "time_exit"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["id"], "close-1")
        self.assertEqual(len(client.payloads), 1)

    def test_close_order_plans_keep_pending_active_on_status_lookup_failure(self):
        client = _Client()
        client.statuses["close-pending"] = TimeoutError("broker unavailable")
        position = _position()
        position.pending_close_order_id = "close-pending"
        position.pending_close_submitted_at = datetime.now(timezone.utc) - timedelta(minutes=30)

        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "stop_loss"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["status"], "skipped_pending_close")
        self.assertEqual(client.payloads, [])
        self.assertEqual(position.pending_close_order_id, "close-pending")

    def test_close_order_plans_keep_pending_active_without_status_api(self):
        client = _NoStatusClient()
        position = _position()
        position.pending_close_order_id = "close-pending"
        position.pending_close_submitted_at = datetime.now(timezone.utc) - timedelta(minutes=30)

        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "stop_loss"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["status"], "skipped_pending_close")
        self.assertEqual(client.payloads, [])
        self.assertEqual(position.pending_close_order_id, "close-pending")

    def test_close_order_plans_mark_filled_pending_close_as_closed(self):
        client = _Client()
        client.statuses["close-pending"] = {
            "status": "filled",
            "legs": [
                {"symbol": "SPY260619P00500000", "filled_avg_price": 0.5},
                {"symbol": "SPY260619P00499000", "filled_avg_price": 0.4},
            ],
        }
        position = _position()
        position.pending_close_order_id = "close-pending"
        position.pending_close_action = "take_profit"
        position.pending_close_submitted_at = datetime.now(timezone.utc) - timedelta(minutes=30)

        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "stop_loss"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["status"], "reconciled_closed")
        self.assertEqual(client.payloads, [])
        self.assertIsNotNone(position.closed_at)
        self.assertEqual(position.close_order_id, "close-pending")
        self.assertEqual(position.close_fill_prices["SPY260619P00500000"], 0.5)

    def test_close_order_plans_mark_immediate_filled_submit_as_closed(self):
        client = _Client(submit_status="filled")
        position = _position()
        client.submit_order = lambda payload: {
            "id": "close-1",
            "status": "filled",
            "legs": [
                {"symbol": "SPY260619P00500000", "filled_avg_price": 0.5},
                {"symbol": "SPY260619P00499000", "filled_avg_price": 0.4},
            ],
        }

        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "take_profit"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["status"], "filled")
        self.assertIsNotNone(position.closed_at)
        self.assertEqual(position.close_order_id, "close-1")
        self.assertEqual(position.pending_close_order_id, "")

    def test_close_order_plans_keeps_status_only_filled_close_pending(self):
        client = _Client()
        client.statuses["close-pending"] = "filled"
        position = _position()
        position.pending_close_order_id = "close-pending"
        position.pending_close_action = "take_profit"
        position.pending_close_submitted_at = datetime.now(timezone.utc) - timedelta(minutes=30)

        plans = close_order_plans(
            [position],
            [],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans, [])
        self.assertIsNone(position.closed_at)
        self.assertEqual(position.pending_close_order_id, "close-pending")
        self.assertEqual(client.payloads, [])

    def test_close_order_plans_keeps_immediate_status_only_fill_pending(self):
        client = _Client(submit_status="filled")
        position = _position()

        plans = close_order_plans(
            [position],
            [{"strategy_id": "vertical_spread-SPY-20260423", "action": "take_profit"}],
            client=client,
            execute_enabled=True,
            dry_run=False,
        )

        self.assertEqual(plans[0]["result"]["status"], "filled")
        self.assertIsNone(position.closed_at)
        self.assertEqual(position.pending_close_order_id, "close-1")
        self.assertEqual(position.pending_close_action, "take_profit")

    def test_position_snapshot_persists_underlying_price_and_pending_close(self):
        position = _position()
        position.pending_close_order_id = "close-1"
        position.pending_close_action = "take_profit"
        position.pending_close_submitted_at = datetime(2026, 4, 23, 10, 5, tzinfo=timezone.utc)

        loaded = PositionSnapshot.from_dict(position.as_dict())

        self.assertEqual(loaded.legs[0].contract.underlying_price, 520.0)
        self.assertEqual(loaded.pending_close_order_id, "close-1")
        self.assertEqual(loaded.pending_close_action, "take_profit")

    def test_position_snapshot_persists_close_reconciliation(self):
        position = _position()
        position.closed_at = datetime(2026, 4, 23, 10, 6, tzinfo=timezone.utc)
        position.close_order_id = "close-1"
        position.close_action = "take_profit"

        loaded = PositionSnapshot.from_dict(position.as_dict())

        self.assertEqual(loaded.close_order_id, "close-1")
        self.assertEqual(loaded.close_action, "take_profit")
        self.assertIsNotNone(loaded.closed_at)

    def test_mark_strategy_closed_updates_open_trade_log_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.csv"
            append_trade_rows(
                path,
                [
                    {
                        "timestamp": "2026-04-23T10:00:00+00:00",
                        "strategy_id": "vertical_spread-SPY-20260423",
                        "leg_number": 1,
                        "contract_symbol": "SPY260619P00500000",
                        "side": "sell_to_open",
                        "qty": 1,
                        "entry_price": 1.05,
                        "order_id": "open-1",
                        "status": "open",
                    },
                    {
                        "timestamp": "2026-04-23T10:00:00+00:00",
                        "strategy_id": "vertical_spread-SPY-20260423",
                        "leg_number": 2,
                        "contract_symbol": "SPY260619P00499000",
                        "side": "buy_to_open",
                        "qty": 1,
                        "entry_price": 0.85,
                        "order_id": "open-1",
                        "status": "open",
                    },
                ],
            )
            position = _position()
            position.current_pnl = 999.0
            position.closed_at = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
            position.close_order_id = "close-1"
            position.close_fill_prices = {
                "SPY260619P00500000": 0.50,
                "SPY260619P00499000": 0.40,
            }

            updated = mark_strategy_closed(
                path,
                position,
                exit_reason="take_profit",
                closed_at=position.closed_at,
            )
            rows = read_trade_rows(path)

        self.assertEqual(updated, 2)
        self.assertTrue(all(row["status"] == "closed" for row in rows))
        self.assertTrue(all(row["order_id"] == "open-1" for row in rows))
        self.assertTrue(all(row["close_order_id"] == "close-1" for row in rows))
        self.assertTrue(all(row["exit_reason"] == "take_profit" for row in rows))
        self.assertTrue(all(row["timestamp"] == "2026-04-23T10:00:00+00:00" for row in rows))
        self.assertTrue(all(row["close_timestamp"] == "2026-04-24T10:00:00+00:00" for row in rows))
        self.assertEqual(rows[0]["exit_price"], "0.5")
        self.assertEqual(rows[1]["exit_price"], "0.4")
        self.assertEqual(rows[0]["pnl_pct"], "50.0")

    def test_mark_strategy_closed_skips_without_broker_fill_prices(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.csv"
            append_trade_rows(
                path,
                [
                    {
                        "timestamp": "2026-04-23T10:00:00+00:00",
                        "strategy_id": "vertical_spread-SPY-20260423",
                        "leg_number": 1,
                        "contract_symbol": "SPY260619P00500000",
                        "side": "sell_to_open",
                        "qty": 1,
                        "entry_price": 1.05,
                        "order_id": "open-1",
                        "status": "open",
                    },
                ],
            )
            position = _position()
            position.current_pnl = 999.0
            position.closed_at = datetime(2026, 4, 24, 10, 0, tzinfo=timezone.utc)
            position.close_order_id = "close-1"
            position.close_fill_prices = {}

            updated = mark_strategy_closed(
                path,
                position,
                exit_reason="take_profit",
                closed_at=position.closed_at,
            )
            rows = read_trade_rows(path)

        self.assertEqual(updated, 0)
        self.assertEqual(rows[0]["order_id"], "open-1")
        self.assertEqual(rows[0]["status"], "open")
        self.assertEqual(rows[0]["close_order_id"], "")
        self.assertEqual(rows[0]["exit_price"], "")
        self.assertEqual(rows[0]["pnl_pct"], "")

    def test_append_trade_rows_keeps_old_header_columns_aligned(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.csv"
            old_fields = [field for field in TRADE_LOG_FIELDS if field != "close_timestamp"]
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=old_fields)
                writer.writeheader()
            append_trade_rows(
                path,
                [
                    {
                        "timestamp": "2026-04-23T10:00:00+00:00",
                        "mode": "paper",
                        "strategy": "vertical_spread",
                        "status": "open",
                        "close_timestamp": "",
                    }
                ],
            )
            with path.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))

        self.assertEqual(rows[0]["mode"], "paper")
        self.assertEqual(rows[0]["strategy"], "vertical_spread")
        self.assertEqual(rows[0]["status"], "open")


if __name__ == "__main__":
    unittest.main()
