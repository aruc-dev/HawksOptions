# Strategy Notes

## Cash-Secured Put

- Short put only after IV-rank and earnings gates pass
- Full strike cash coverage required
- 50% profit target, 2x credit stop, 21-DTE time exit

## Covered Call

- Only against existing long stock
- Strike must be above spot and cost basis
- Ex-dividend protection closes ITM short calls when dividend exceeds remaining extrinsic value

## Vertical Spread

- Defined-risk credit spread
- Closest protective long leg is preferred when the exact long-delta target would oversize account risk
- Profit target 50%, stop 1.5x credit

## Iron Condor

- High-IV, low-trend regime only
- No untested-side rolling
- 40% profit target, 2x credit stop

## Deferred

- `calendar_spread` and `earnings_iron_condor` are shipped but disabled by default
- Enable only after paper validation and config review
