# Assignment Playbook

## Short Calls

1. Check ex-dividend date daily.
2. If the short call is ITM and dividend exceeds remaining extrinsic value,
   close before ex-dividend.
3. If assignment is detected, mark the resulting stock as covered-call eligible
   or liquidate according to the operator’s policy.

## Short Puts

1. Treat strike less premium as the effective stock entry.
2. If assigned, record the stock position and stop opening overlapping short
   puts on the same underlying.
3. Covered calls may be used only after the stock position exists.
