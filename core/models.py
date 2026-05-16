"""Dataclasses shared across the options trading system."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from typing import Any


def _to_date(value: date | datetime | str | None) -> date | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _to_datetime(value: datetime | str | None) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value
    text = str(value)
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    return datetime.fromisoformat(text)


@dataclass(frozen=True)
class OptionContract:
    contract_symbol: str
    underlying: str
    option_type: str
    strike: float
    expiration: date
    bid: float
    ask: float
    last: float = 0.0
    open_interest: int = 0
    volume: int = 0
    implied_volatility: float = 0.0
    delta: float | None = None
    theta: float | None = None
    vega: float | None = None
    gamma: float | None = None
    underlying_price: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    def mid_price(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return round((self.bid + self.ask) / 2.0, 4)
        if self.last > 0:
            return round(self.last, 4)
        return 0.0

    def spread_pct(self) -> float:
        mid = self.mid_price()
        if mid <= 0:
            return float("inf")
        return round((self.ask - self.bid) / mid, 6)

    def days_to_expiration(self, as_of: date | datetime | None) -> int:
        ref = _to_date(as_of) or date.today()
        return (self.expiration - ref).days

    def is_itm(self) -> bool:
        if self.option_type == "call":
            return self.underlying_price > self.strike
        return self.underlying_price < self.strike


@dataclass(frozen=True)
class OrderLeg:
    contract: OptionContract
    side: str
    qty: int = 1

    def opening_cashflow(self) -> float:
        sign = 1.0 if self.side == "sell_to_open" else -1.0
        return round(sign * self.contract.mid_price() * 100.0 * self.qty, 2)

    def closing_cashflow(self) -> float:
        sign = -1.0 if self.side == "sell_to_open" else 1.0
        return round(sign * self.contract.mid_price() * 100.0 * self.qty, 2)


@dataclass
class StrategyOrder:
    strategy_name: str
    strategy_id: str
    underlying: str
    legs: list[OrderLeg]
    max_loss: float
    max_profit: float
    required_buying_power: float
    profit_take_pct: float
    loss_stop_multiple: float
    roll_threshold_delta: float | None
    iv_rank: float
    required_options_level: int = 1
    swing_only: bool = True
    next_earnings_date: date | None = None
    ex_dividend_date: date | None = None
    ai_veto_reason: str = ""
    notes: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def net_opening_credit(self) -> float:
        return round(sum(leg.opening_cashflow() for leg in self.legs), 2)

    @property
    def min_dte(self) -> int:
        if not self.legs:
            return 0
        return min(leg.contract.days_to_expiration(date.today()) for leg in self.legs)

    @property
    def short_legs(self) -> list[OrderLeg]:
        return [leg for leg in self.legs if leg.side == "sell_to_open"]

    @property
    def long_legs(self) -> list[OrderLeg]:
        return [leg for leg in self.legs if leg.side == "buy_to_open"]

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["legs"] = [
            {
                "side": leg.side,
                "qty": leg.qty,
                "contract": {
                    **asdict(leg.contract),
                    "expiration": leg.contract.expiration.isoformat(),
                },
            }
            for leg in self.legs
        ]
        if self.next_earnings_date is not None:
            payload["next_earnings_date"] = self.next_earnings_date.isoformat()
        if self.ex_dividend_date is not None:
            payload["ex_dividend_date"] = self.ex_dividend_date.isoformat()
        return payload


@dataclass
class PositionSnapshot:
    strategy_id: str
    strategy_name: str
    underlying: str
    legs: list[OrderLeg]
    opened_at: datetime
    entry_credit: float
    max_loss: float
    profit_take_pct: float
    loss_stop_multiple: float
    roll_threshold_delta: float | None
    current_close_cost: float = 0.0
    current_pnl: float = 0.0
    next_earnings_date: date | None = None
    ex_dividend_date: date | None = None
    dividend_amount: float = 0.0
    remaining_extrinsic_value: float = 0.0
    short_leg_itm: bool = False
    roll_count: int = 0

    @property
    def days_to_expiration(self) -> int:
        return min((leg.contract.days_to_expiration(date.today()) for leg in self.legs), default=0)

    @property
    def short_delta(self) -> float:
        deltas = [leg.contract.delta for leg in self.legs if leg.side == "sell_to_open" and leg.contract.delta is not None]
        if not deltas:
            return 0.0
        return sorted(deltas, key=abs, reverse=True)[0]

    def as_dict(self) -> dict[str, Any]:
        payload = {
            "strategy_id": self.strategy_id,
            "strategy_name": self.strategy_name,
            "underlying": self.underlying,
            "opened_at": self.opened_at.isoformat(timespec="seconds"),
            "entry_credit": round(self.entry_credit, 2),
            "max_loss": round(self.max_loss, 2),
            "profit_take_pct": self.profit_take_pct,
            "loss_stop_multiple": self.loss_stop_multiple,
            "roll_threshold_delta": self.roll_threshold_delta,
            "current_close_cost": round(self.current_close_cost, 2),
            "current_pnl": round(self.current_pnl, 2),
            "days_to_expiration": self.days_to_expiration,
            "short_delta": self.short_delta,
            "dividend_amount": self.dividend_amount,
            "remaining_extrinsic_value": round(self.remaining_extrinsic_value, 2),
            "short_leg_itm": self.short_leg_itm,
            "roll_count": self.roll_count,
            "legs": [
                {
                    "side": leg.side,
                    "qty": leg.qty,
                    "contract_symbol": leg.contract.contract_symbol,
                    "option_type": leg.contract.option_type,
                    "strike": leg.contract.strike,
                    "expiration": leg.contract.expiration.isoformat(),
                    "delta": leg.contract.delta,
                    "theta": leg.contract.theta,
                    "vega": leg.contract.vega,
                    "gamma": leg.contract.gamma,
                    "mid_price": leg.contract.mid_price(),
                }
                for leg in self.legs
            ],
        }
        if self.next_earnings_date is not None:
            payload["next_earnings_date"] = self.next_earnings_date.isoformat()
        if self.ex_dividend_date is not None:
            payload["ex_dividend_date"] = self.ex_dividend_date.isoformat()
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "PositionSnapshot":
        legs = []
        for item in payload.get("legs", []):
            contract = OptionContract(
                contract_symbol=str(item.get("contract_symbol", "")),
                underlying=str(payload.get("underlying", "")),
                option_type=str(item.get("option_type", "")),
                strike=float(item.get("strike", 0.0)),
                expiration=_to_date(item.get("expiration")) or date.today(),
                bid=float(item.get("mid_price", 0.0)),
                ask=float(item.get("mid_price", 0.0)),
                delta=item.get("delta"),
                theta=item.get("theta"),
                vega=item.get("vega"),
                gamma=item.get("gamma"),
            )
            legs.append(OrderLeg(contract=contract, side=str(item.get("side", "")), qty=int(item.get("qty", 1))))
        return cls(
            strategy_id=str(payload.get("strategy_id", "")),
            strategy_name=str(payload.get("strategy_name", "")),
            underlying=str(payload.get("underlying", "")),
            legs=legs,
            opened_at=_to_datetime(payload.get("opened_at")) or datetime.utcnow(),
            entry_credit=float(payload.get("entry_credit", 0.0)),
            max_loss=float(payload.get("max_loss", 0.0)),
            profit_take_pct=float(payload.get("profit_take_pct", 0.0)),
            loss_stop_multiple=float(payload.get("loss_stop_multiple", 0.0)),
            roll_threshold_delta=payload.get("roll_threshold_delta"),
            current_close_cost=float(payload.get("current_close_cost", 0.0)),
            current_pnl=float(payload.get("current_pnl", 0.0)),
            next_earnings_date=_to_date(payload.get("next_earnings_date")),
            ex_dividend_date=_to_date(payload.get("ex_dividend_date")),
            dividend_amount=float(payload.get("dividend_amount", 0.0)),
            remaining_extrinsic_value=float(payload.get("remaining_extrinsic_value", 0.0)),
            short_leg_itm=bool(payload.get("short_leg_itm", False)),
            roll_count=int(payload.get("roll_count", 0)),
        )


@dataclass(frozen=True)
class StrategyContext:
    underlying: dict[str, Any]
    chain: list[OptionContract]
    config: dict[str, Any]
    account: dict[str, Any]
    iv_rank: float
    as_of: date
    underlying_price: float
    current_iv: float = 0.0
    iv_percentile: float = 0.0
    next_earnings_date: date | None = None
    ex_dividend_date: date | None = None
    dividend_amount: float = 0.0
    realized_vol_20d: float = 0.0
    atr_pct: float = 0.0
    trend_20d: float | None = None
    trend_50d: float | None = None
    rsi_14: float | None = None
    price_vs_sma_50: float | None = None
    long_shares: int = 0
    cost_basis: float = 0.0
    open_positions: tuple[PositionSnapshot, ...] = ()
