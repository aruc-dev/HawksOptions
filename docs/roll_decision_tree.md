# Roll Decision Tree

1. Has the short-leg delta breached the configured roll threshold?
   If no, do not roll.
2. Is the position already at the max-roll cap?
   If yes, do not roll.
3. Is a replacement order available at a later expiry?
   If no, close instead of forcing a bad roll.
4. Does the close-plus-reopen sequence collect a net credit?
   If no, close instead of rolling for a debit.
5. If all checks pass, roll down/out or up/out according to the strategy.
