from __future__ import annotations

import unittest
from datetime import date, datetime, timedelta, timezone

from core.close_executor import build_close_order_payload, close_order_plans
from core.models import OptionContract, OrderLeg, PositionSnapshot


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


class CloseExecutorTests(unittest.TestCase):
    def test_build_close_order_payload_uses_broker_close_sides(self):
        payload = build_close_order_payload(_position())

        self.assertEqual(payload["position_intent"], "close")
        self.assertEqual(payload["order_class"], "mleg")
        self.assertEqual(payload["legs"][0]["side"], "buy")
        self.assertEqual(payload["legs"][1]["side"], "sell")
        self.assertEqual(payload["limit_price"], 0.2)

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

    def test_build_close_order_payload_normalizes_limit_by_position_quantity(self):
        payload = build_close_order_payload(_position(qty=3))

        self.assertEqual(payload["limit_price"], 0.2)

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

    def test_close_order_plans_mark_filled_pending_close_as_closed(self):
        client = _Client()
        client.statuses["close-pending"] = "filled"
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

        self.assertEqual(plans[0]["result"]["status"], "skipped_closed")
        self.assertEqual(client.payloads, [])
        self.assertIsNotNone(position.closed_at)
        self.assertEqual(position.close_order_id, "close-pending")

    def test_close_order_plans_mark_immediate_filled_submit_as_closed(self):
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
        self.assertIsNotNone(position.closed_at)
        self.assertEqual(position.close_order_id, "close-1")
        self.assertEqual(position.pending_close_order_id, "")

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


if __name__ == "__main__":
    unittest.main()
