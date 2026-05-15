(function () {
  async function loadState() {
    const res = await fetch("/api/state", { credentials: "same-origin", cache: "no-store" });
    const data = await res.json();
    document.getElementById("overview-status").textContent =
      "Open strategies: " + (data.open_strategies || []).length +
      " • Reachable: " + (data.alpaca_reachable ? "yes" : "no");
    document.getElementById("positions-json").textContent = JSON.stringify(data.open_strategies || [], null, 2);
    document.getElementById("greeks-json").textContent = JSON.stringify(data.portfolio_greeks || {}, null, 2);
    const analytics = data.analytics || {};
    document.getElementById("candidate-funnel-json").textContent =
      JSON.stringify(analytics.candidate_funnel || {}, null, 2);
    document.getElementById("risk-budget-json").textContent =
      JSON.stringify(analytics.risk_budget || {}, null, 2);
    document.getElementById("strategy-attribution-json").textContent =
      JSON.stringify(analytics.strategy_attribution || {}, null, 2);
    document.getElementById("drift-json").textContent =
      JSON.stringify(analytics.drift || {}, null, 2);
    document.getElementById("scan-health-json").textContent =
      JSON.stringify(analytics.scan_health || {}, null, 2);
    document.getElementById("research-trace-json").textContent =
      JSON.stringify(analytics.research_trace || {}, null, 2);
    document.getElementById("ai-disagreements-json").textContent =
      JSON.stringify(analytics.ai_disagreements || {}, null, 2);
  }

  loadState().catch((error) => {
    document.getElementById("overview-status").textContent = "Dashboard fetch failed: " + error.message;
  });
})();
