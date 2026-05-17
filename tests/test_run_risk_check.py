from __future__ import annotations

import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from core.models import OptionContract, OrderLeg, PositionSnapshot
from scheduler.run_risk_check import run_risk_check


def _position() -> PositionSnapshot:
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
            OrderLeg(short, "sell_to_open"),
            OrderLeg(long, "buy_to_open"),
        ],
        opened_at=datetime(2026, 4, 23, 10, 0, tzinfo=timezone.utc),
        entry_credit=20.0,
        max_loss=80.0,
        profit_take_pct=0.5,
        loss_stop_multiple=1.5,
        roll_threshold_delta=-0.4,
        pending_close_order_id="close-pending",
        pending_close_action="take_profit",
        pending_close_submitted_at=datetime(2026, 4, 23, 10, 5, tzinfo=timezone.utc),
    )


class _Client:
    def get_order_status(self, order_id):
        return {
            "status": "filled",
            "legs": [
                {"symbol": "SPY260619P00500000", "filled_avg_price": 0.5},
                {"symbol": "SPY260619P00499000", "filled_avg_price": 0.4},
            ],
        }

    def get_account(self):
        return {"portfolio_value": 10000.0}


class RunRiskCheckTests(unittest.TestCase):
    def _run_with_reconciled_pending_close(self, *, dry_run: bool):
        config = {
            "risk_actions": {"execute_closes": False},
            "account": {},
        }
        paths = {
            "positions": Path("positions.json"),
            "trade_log": Path("trades.csv"),
            "baseline": Path("baseline.json"),
            "greeks_dir": Path("greeks"),
        }
        position = _position()
        with (
            patch("scheduler.run_risk_check.load_runtime", return_value=(config, _Client(), paths)),
            patch("scheduler.run_risk_check.current_positions", return_value=[position]),
            patch("scheduler.run_risk_check.refresh_positions", side_effect=lambda positions, **_: positions),
            patch("scheduler.run_risk_check.continuous_risk_checks", return_value={"actions": []}),
            patch("scheduler.run_risk_check.read_daily_baseline", return_value={"date": "2026-04-23", "portfolio_value": 10000.0}),
            patch("scheduler.run_risk_check.write_daily_baseline") as write_daily_baseline,
            patch("scheduler.run_risk_check.save_positions") as save_positions,
            patch("scheduler.run_risk_check.mark_strategy_closed") as mark_strategy_closed,
            patch("scheduler.run_risk_check.write_greeks_snapshot") as write_greeks_snapshot,
        ):
            write_greeks_snapshot.return_value = Path("snapshot.json")
            result = run_risk_check(as_of=date(2026, 4, 23), dry_run=dry_run)
        return result, save_positions, mark_strategy_closed, write_daily_baseline, write_greeks_snapshot

    def test_dry_run_pending_close_reconciliation_does_not_persist(self):
        result, save_positions, mark_strategy_closed, write_daily_baseline, write_greeks_snapshot = self._run_with_reconciled_pending_close(dry_run=True)

        self.assertEqual(result["pending_close_reconciliations"][0]["result"]["status"], "reconciled_closed")
        save_positions.assert_not_called()
        mark_strategy_closed.assert_not_called()
        write_daily_baseline.assert_not_called()
        write_greeks_snapshot.assert_not_called()
        self.assertEqual(result["snapshot_path"], "")

    def test_reconciled_pending_close_updates_trade_log_even_when_new_closes_disabled(self):
        result, save_positions, mark_strategy_closed, write_daily_baseline, write_greeks_snapshot = self._run_with_reconciled_pending_close(dry_run=False)

        self.assertEqual(result["pending_close_reconciliations"][0]["result"]["status"], "reconciled_closed")
        save_positions.assert_called_once()
        mark_strategy_closed.assert_called_once()
        write_daily_baseline.assert_not_called()
        write_greeks_snapshot.assert_called_once()

    def test_dry_run_uses_transient_daily_baseline_when_missing(self):
        config = {"risk_actions": {"execute_closes": False}, "account": {}}
        paths = {
            "positions": Path("positions.json"),
            "trade_log": Path("trades.csv"),
            "baseline": Path("baseline.json"),
            "greeks_dir": Path("greeks"),
        }
        with (
            patch("scheduler.run_risk_check.load_runtime", return_value=(config, _Client(), paths)),
            patch("scheduler.run_risk_check.current_positions", return_value=[]),
            patch("scheduler.run_risk_check.refresh_positions", return_value=[]),
            patch("scheduler.run_risk_check.continuous_risk_checks", return_value={"actions": []}),
            patch("scheduler.run_risk_check.read_daily_baseline", return_value=None),
            patch("scheduler.run_risk_check.write_daily_baseline") as write_daily_baseline,
            patch("scheduler.run_risk_check.write_greeks_snapshot") as write_greeks_snapshot,
        ):
            result = run_risk_check(as_of=date(2026, 4, 23), dry_run=True)

        self.assertEqual(result["daily_loss"]["status"], "ok")
        self.assertEqual(result["snapshot_path"], "")
        write_daily_baseline.assert_not_called()
        write_greeks_snapshot.assert_not_called()


if __name__ == "__main__":
    unittest.main()
