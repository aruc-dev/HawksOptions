"""Concurrency tests for shared state files.

These tests simulate the production scheduler topology where a 1-min,
5-min, and 30-min job may all touch positions.json or trade_log.csv at
the same time. Without locking those files were vulnerable to
interleaved or duplicated writes (header lines, half-written JSON).
The locking added in core/file_lock.py and threaded through
core/order_executor.py and core/trade_log.py should keep the files
valid under contention.

We use multiprocessing because thread-level locks would not catch
fcntl/flock edge cases; flock works at the process level.
"""

from __future__ import annotations

import csv
import json
import multiprocessing
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import core.file_lock as file_lock
from core.iv_rank_tracker import append_iv_snapshot, load_iv_history, prune_iv_history
from core.order_executor import load_positions, persist_open_order, save_positions
from core.trade_log import TRADE_LOG_FIELDS, append_trade_rows, read_trade_rows


def _append_trade_worker(args):
    path, batch_id, n = args
    rows = [
        {
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mode": "paper",
            "strategy": "vertical_spread",
            "underlying": "SPY",
            "strategy_id": f"batch-{batch_id}",
            "leg_number": i,
            "contract_symbol": f"SYM-{batch_id}-{i}",
            "option_type": "put",
            "strike": 500.0,
            "expiration": "2026-06-19",
            "dte_at_entry": 30,
            "side": "sell_to_open",
            "qty": 1,
            "entry_price": 1.0,
            "credit_received_per_spread": 50.0,
            "max_loss_per_spread": 200.0,
            "stop_loss": 75.0,
            "take_profit": 25.0,
            "order_id": f"order-{batch_id}-{i}",
            "status": "open",
        }
        for i in range(n)
    ]
    append_trade_rows(path, rows)
    return batch_id


def _save_positions_worker(args):
    path, payload = args
    # Re-import inside the worker so multiprocessing on macOS (spawn)
    # can pickle.
    from core.models import OptionContract, OrderLeg, PositionSnapshot
    from datetime import date

    positions = []
    for entry in payload:
        contract = OptionContract(
            contract_symbol=entry["sym"],
            underlying=entry["und"],
            option_type="put",
            strike=500.0,
            expiration=date(2026, 6, 19),
            bid=1.0,
            ask=1.05,
        )
        positions.append(
            PositionSnapshot(
                strategy_id=entry["sym"],
                strategy_name="vertical_spread",
                underlying=entry["und"],
                legs=[OrderLeg(contract=contract, side="sell_to_open")],
                opened_at=datetime.now(timezone.utc),
                entry_credit=50.0,
                max_loss=200.0,
                profit_take_pct=0.5,
                loss_stop_multiple=2.0,
                roll_threshold_delta=-0.4,
            )
        )
    save_positions(Path(path), positions)
    return payload[-1]["sym"] if payload else None


def _persist_order_worker(args):
    trade_log_path, positions_path, idx = args
    from core.models import OptionContract, OrderLeg, StrategyOrder
    from datetime import date

    contract = OptionContract(
        contract_symbol=f"SPY260619P00{idx:06d}",
        underlying="SPY",
        option_type="put",
        strike=500.0 + idx,
        expiration=date(2026, 6, 19),
        bid=1.0,
        ask=1.05,
        open_interest=500,
        volume=50,
        underlying_price=520.0,
    )
    order = StrategyOrder(
        strategy_name="vertical_spread",
        strategy_id=f"persist-{idx}",
        underlying="SPY",
        legs=[OrderLeg(contract=contract, side="sell_to_open")],
        max_loss=200.0,
        max_profit=100.0,
        required_buying_power=200.0,
        profit_take_pct=0.5,
        loss_stop_multiple=2.0,
        roll_threshold_delta=-0.4,
        iv_rank=50.0,
    )
    persist_open_order(
        order=order,
        mode="paper",
        order_id=f"order-{idx}",
        trade_log_path=Path(trade_log_path),
        positions_path=Path(positions_path),
    )
    return idx


def _append_iv_worker(args):
    path, symbol, n = args
    for i in range(n):
        append_iv_snapshot(Path(path), symbol, 0.20 + 0.001 * i)
    return symbol


class ConcurrencyTests(unittest.TestCase):
    def test_locked_open_flushes_writer_before_unlocking(self):
        """A waiting process must see writes before it acquires the lock."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "locked.txt"
            observed_sizes: list[int] = []
            original_release = file_lock._release

            def release_with_size_check(handle):
                observed_sizes.append(path.stat().st_size)
                original_release(handle)

            file_lock._release = release_with_size_check
            try:
                with file_lock.locked_open(path, "a", lock="exclusive") as handle:
                    handle.write("x")
            finally:
                file_lock._release = original_release

            self.assertEqual(observed_sizes, [1])

    def test_locked_open_write_mode_truncates_after_locking(self):
        """Opening in write mode must not truncate before the lock is acquired."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "locked.txt"
            path.write_text("existing", encoding="utf-8")
            observed_payloads: list[str] = []
            original_acquire = file_lock._acquire

            def acquire_with_payload_check(handle, mode):
                observed_payloads.append(path.read_text(encoding="utf-8"))
                original_acquire(handle, mode)

            file_lock._acquire = acquire_with_payload_check
            try:
                with file_lock.locked_open(path, "w", lock="exclusive") as handle:
                    handle.write("replacement")
            finally:
                file_lock._acquire = original_acquire

            self.assertEqual(observed_payloads, ["existing"])
            self.assertEqual(path.read_text(encoding="utf-8"), "replacement")

    def test_iv_append_waits_for_history_sidecar_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "iv.csv"
            with file_lock.locked_open(file_lock.lock_path_for(path), "a", lock="exclusive"):
                process = multiprocessing.Process(
                    target=_append_iv_worker,
                    args=((str(path), "SPY", 1),),
                )
                process.start()
                process.join(0.25)
                blocked_on_lock = process.is_alive()

            process.join(5)
            self.assertTrue(blocked_on_lock)
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(load_iv_history(path, "SPY"), [0.2])

    def test_concurrent_trade_log_appends_keep_one_header(self):
        """Multiple processes appending simultaneously must produce a
        single header row and the expected total row count."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "trades.csv"
            n_workers = 6
            n_rows = 5
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(processes=n_workers) as pool:
                pool.map(
                    _append_trade_worker,
                    [(path, idx, n_rows) for idx in range(n_workers)],
                )
            rows = read_trade_rows(path)
            # Total rows = workers * rows_per_worker.
            self.assertEqual(len(rows), n_workers * n_rows)
            # Verify the file has exactly one header line.
            with open(path, "r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
            header = ",".join(TRADE_LOG_FIELDS)
            self.assertEqual(lines[0], header)
            self.assertEqual(lines.count(header), 1)
            # All rows have valid status values (no torn writes).
            for row in rows:
                self.assertEqual(row["status"], "open")

    def test_concurrent_positions_writes_remain_valid_json(self):
        """save_positions uses atomic_write_text under an exclusive
        lock. After many concurrent writes the file must still parse
        as a JSON list."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.json"
            payloads = [
                [{"sym": f"S{idx}-{i}", "und": f"U{idx}"} for i in range(3)]
                for idx in range(8)
            ]
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(processes=4) as pool:
                pool.map(_save_positions_worker, [(str(path), p) for p in payloads])
            # Whatever ended up on disk must be a valid list of
            # PositionSnapshot dicts. We don't care which writer won
            # only that the file isn't half-written.
            loaded = load_positions(path)
            self.assertIsInstance(loaded, list)
            # The file must be parseable as JSON too.
            with open(path, "r", encoding="utf-8") as handle:
                parsed = json.load(handle)
            self.assertIsInstance(parsed, list)

    def test_save_positions_worker_handles_empty_payload(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "positions.json"

            result = _save_positions_worker((str(path), []))

            self.assertIsNone(result)
            self.assertEqual(load_positions(path), [])

    def test_concurrent_persist_open_order_keeps_all_positions(self):
        with tempfile.TemporaryDirectory() as tmp:
            trade_log_path = Path(tmp) / "trades.csv"
            positions_path = Path(tmp) / "positions.json"
            n_workers = 8
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(processes=4) as pool:
                pool.map(
                    _persist_order_worker,
                    [(str(trade_log_path), str(positions_path), idx) for idx in range(n_workers)],
                )

            positions = load_positions(positions_path)
            self.assertEqual(len(positions), n_workers)
            self.assertEqual(
                {position.strategy_id for position in positions},
                {f"persist-{idx}" for idx in range(n_workers)},
            )

    def test_concurrent_iv_snapshot_appends(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "iv.csv"
            ctx = multiprocessing.get_context("spawn")
            with ctx.Pool(processes=4) as pool:
                pool.map(
                    _append_iv_worker,
                    [(str(path), "SPY", 10), (str(path), "QQQ", 10), (str(path), "IWM", 10), (str(path), "DIA", 10)],
                )
            with open(path, "r", encoding="utf-8") as handle:
                rows = list(csv.reader(handle))
            # 1 header + 40 data rows.
            self.assertEqual(len(rows), 41)
            self.assertEqual(rows[0], ["timestamp", "symbol", "implied_volatility"])

    def test_iv_history_pruning(self):
        """prune_iv_history drops rows older than the cutoff and
        preserves recent ones. Atomic rewrite must keep the file
        readable throughout."""
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "iv.csv"
            now = datetime.now(timezone.utc)
            # Manually craft history with a mix of old and new rows.
            with open(path, "w", encoding="utf-8", newline="") as handle:
                writer = csv.writer(handle)
                writer.writerow(["timestamp", "symbol", "implied_volatility"])
                # Old row (400 days ago).
                writer.writerow([
                    (now.replace(year=now.year - 2)).isoformat(timespec="seconds"),
                    "SPY",
                    "0.18",
                ])
                # Recent row.
                writer.writerow([now.isoformat(timespec="seconds"), "SPY", "0.22"])
            dropped = prune_iv_history(path, max_age_days=365)
            self.assertEqual(dropped, 1)
            with open(path, "r", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["implied_volatility"], "0.22")


if __name__ == "__main__":
    unittest.main()
