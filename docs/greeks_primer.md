# Greeks Primer

## Delta

Directional exposure. In HawksOptions, short-option delta contributes with the
opposite sign because the account is short the contract.

## Theta

Time decay. Short-premium trades usually want positive net theta.

## Vega

Sensitivity to implied volatility. Short-premium books are short vega and can
lose even when spot barely moves during a volatility expansion.

## Gamma

How fast delta changes. This is why the system exits before expiry and refuses
0-DTE or 1-DTE entries.
