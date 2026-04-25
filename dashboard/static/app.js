(function () {
  async function loadState() {
    const res = await fetch("/api/state", { credentials: "same-origin", cache: "no-store" });
    const data = await res.json();
    document.getElementById("overview-status").textContent =
      "Open strategies: " + (data.open_strategies || []).length +
      " • Reachable: " + (data.alpaca_reachable ? "yes" : "no");
    document.getElementById("positions-json").textContent = JSON.stringify(data.open_strategies || [], null, 2);
    document.getElementById("greeks-json").textContent = JSON.stringify(data.portfolio_greeks || {}, null, 2);
  }

  loadState().catch((error) => {
    document.getElementById("overview-status").textContent = "Dashboard fetch failed: " + error.message;
  });
})();
