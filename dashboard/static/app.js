// HawksOptions dashboard client - polling, read-only.
(function () {
  const POLL_MS = 15000;
  const STALE_RED_MS = 60000;
  let lastStateMs = 0;
  let lastPollError = "";

  const $ = (id) => document.getElementById(id);
  const hasNumber = (n) => n !== null && n !== undefined && n !== "" && Number.isFinite(Number(n));
  const number = (n) => hasNumber(n) ? Number(n) : 0;

  const money = (n, signed) => {
    if (!hasNumber(n)) return "-";
    const v = Number(n);
    const sign = v > 0 && signed ? "+" : "";
    return sign + "$" + v.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  };

  const compactMoney = (n, signed) => {
    if (!hasNumber(n)) return "-";
    const v = Number(n);
    const sign = v > 0 && signed ? "+" : "";
    return sign + "$" + v.toLocaleString(undefined, {
      notation: Math.abs(v) >= 100000 ? "compact" : "standard",
      maximumFractionDigits: Math.abs(v) >= 100000 ? 1 : 2,
      minimumFractionDigits: Math.abs(v) >= 100000 ? 0 : 2,
    });
  };

  const pct = (n, signed) => {
    if (!hasNumber(n)) return "-";
    const v = Number(n) * 100;
    const sign = v > 0 && signed ? "+" : "";
    return sign + v.toFixed(2) + "%";
  };

  const rawPct = (n) => hasNumber(n) ? Number(n).toFixed(1) + "%" : "-";
  const fixed = (n, digits) => hasNumber(n) ? Number(n).toFixed(digits) : "-";
  const integer = (n) => hasNumber(n) ? String(Math.round(Number(n))) : "-";

  const colorFor = (v) => {
    const x = Number(v || 0);
    if (x > 0) return "text-emerald-400";
    if (x < 0) return "text-rose-400";
    return "text-slate-300";
  };

  const statusColor = (status) => ({
    green: "bg-emerald-400",
    ok: "bg-emerald-400",
    yellow: "bg-amber-400",
    warn: "bg-amber-400",
    red: "bg-rose-500",
    critical: "bg-rose-500",
    tripped: "bg-rose-500",
  }[String(status || "").toLowerCase()] || "bg-slate-600");

  async function fetchState() {
    try {
      const res = await fetch("/api/state", { credentials: "same-origin", cache: "no-store" });
      if (!res.ok) throw new Error("HTTP " + res.status);
      const data = await res.json();
      render(data);
      lastStateMs = Date.now();
      lastPollError = "";
    } catch (error) {
      lastPollError = "fetch error: " + error.message;
      $("refresh-status").textContent = lastPollError;
      $("refresh-status").className = "text-rose-400";
    }
    updateRefreshTicker();
  }

  function updateRefreshTicker() {
    if (lastPollError) {
      $("refresh-status").textContent = lastPollError;
      $("refresh-status").className = "text-rose-400";
      return;
    }
    if (!lastStateMs) return;
    const age = Date.now() - lastStateMs;
    const seconds = Math.floor(age / 1000);
    const el = $("refresh-status");
    el.textContent = "refreshed " + seconds + "s ago";
    el.className = age > STALE_RED_MS ? "text-rose-400" : "text-slate-400";
  }

  function render(state) {
    if (state.ok === false) {
      throw new Error(state.error || "state unavailable");
    }

    const account = state.account || {};
    const analytics = state.analytics || {};
    const riskBudget = analytics.risk_budget || {};
    const headroom = state.daily_loss_headroom || {};
    const funnel = analytics.candidate_funnel || {};
    const health = state.health || {};

    $("mode-badge").textContent = String(state.mode || "?").toUpperCase();
    const healthStatus = state.alpaca_reachable === false
      ? "red"
      : String(health.status || "green").toLowerCase();
    $("health-dot").className = "ho-status-dot " + statusColor(healthStatus);

    renderAccount(account, riskBudget, headroom, state, funnel);
    renderHeadroom(headroom);
    renderOpenStrategies(state.open_strategies || []);
    renderGreeks(state.portfolio_greeks || {});
    renderHealth(health, state.alpaca_reachable);
    renderRiskBudget(riskBudget);
    renderFunnel(funnel, state.rejections || {});
    renderScanQuality(analytics.scan_health || {});
    renderAiResearch(analytics, state.ai_activity || {});
    renderAttribution(analytics.strategy_attribution || {});
    renderIvRanks(state.iv_rank_heatmap || []);
    renderEarnings(state.upcoming_earnings || []);
    renderDrift(analytics.drift || {});
  }

  function renderAccount(account, riskBudget, headroom, state, funnel) {
    $("acct-portfolio").textContent = compactMoney(account.portfolio_value);
    $("acct-cash").textContent = compactMoney(account.cash);
    $("acct-buying-power").textContent = compactMoney(account.buying_power);

    $("risk-open").textContent = compactMoney(riskBudget.open_risk);
    $("risk-open-detail").textContent = "remaining " + compactMoney(riskBudget.portfolio_cap_remaining);

    const today = state.realized_today || {};
    $("realized-today").textContent = money(today.total_usd, true);
    $("realized-today").className = "ho-kpi-value " + colorFor(today.total_usd);
    $("realized-today-detail").textContent = (today.trade_count || 0) + " trades";

    const window30 = state.realized_30d || {};
    $("realized-30d").textContent = money(window30.total_usd, true);
    $("realized-30d").className = "ho-kpi-value " + colorFor(window30.total_usd);
    $("realized-30d-detail").textContent =
      (window30.trade_count || 0) + " trades • " + (window30.wins || 0) + "W / " + (window30.losses || 0) + "L";

    $("candidate-counts").textContent = (funnel.accepted_count || 0) + " / " + (funnel.candidate_count || 0);
    $("candidate-detail").textContent = (funnel.rejected_count || 0) + " rejected • " + (funnel.research_candidate_count || 0) + " research";
  }

  function renderHeadroom(headroom) {
    const limit = number(headroom.limit_usd);
    const loss = Math.max(0, -number(headroom.delta_usd));
    const used = limit > 0 ? Math.min(100, Math.round((loss / limit) * 100)) : 0;
    const status = String(headroom.status || "unknown").toLowerCase();
    const bar = $("headroom-bar");
    bar.style.width = used + "%";
    bar.className = "ho-progress-bar " + ({
      ok: "bg-emerald-500",
      warn: "bg-amber-400",
      critical: "bg-rose-500",
      tripped: "bg-rose-500",
    }[status] || "bg-slate-600");
    $("headroom-status").textContent = status;
    $("headroom-status").dataset.status = status;
    $("headroom-text").innerHTML =
      "baseline " + money(headroom.baseline_value) +
      " -> current <span class=\"" + colorFor(headroom.delta_usd) + "\">" +
      money(headroom.delta_usd, true) + " (" + pct(headroom.delta_pct, true) + ")</span>" +
      " • remaining " + money(headroom.remaining_usd);
  }

  function renderOpenStrategies(strategies) {
    $("strategy-count").textContent = String(strategies.length);
    $("strategies-tbody").innerHTML = strategies.map((s) => {
      const pnlClass = colorFor(s.current_pnl);
      return "<tr>" +
        "<td class=\"text-left\">" + escapeHtml(s.underlying || "-") + "</td>" +
        "<td class=\"text-left\">" + escapeHtml(labelize(s.strategy_name || "unknown")) + "</td>" +
        "<td class=\"text-right mono\">" + (hasNumber(s.days_to_expiration) ? Number(s.days_to_expiration).toFixed(0) : "-") + "</td>" +
        "<td class=\"text-right mono\">" + money(s.entry_credit) + "</td>" +
        "<td class=\"text-right mono\">" + money(s.current_close_cost) + "</td>" +
        "<td class=\"text-right mono " + pnlClass + "\">" + money(s.current_pnl, true) + "</td>" +
        "<td class=\"text-right mono\">" + fixed(s.short_delta, 3) + "</td>" +
        "<td class=\"text-left\">" + escapeHtml(s.next_earnings_date || "-") + "</td>" +
      "</tr>";
    }).join("") || "<tr><td colspan=\"8\" class=\"ho-empty-cell\">No open options strategies</td></tr>";
  }

  function renderGreeks(greeks) {
    setText("greek-delta", fixed(greeks.delta, 2));
    setText("greek-theta", fixed(greeks.theta, 2));
    setText("greek-vega", fixed(greeks.vega, 2));
    setText("greek-gamma", fixed(greeks.gamma, 4));
    $("greeks-status").textContent = Object.keys(greeks || {}).length ? "updated" : "empty";
  }

  function renderHealth(health, alpacaReachable) {
    const status = String(health.status || "unknown").toLowerCase();
    const effectiveStatus = alpacaReachable === false ? "red" : status;
    const el = $("health-status");
    el.textContent = (effectiveStatus === "unknown" ? "?" : effectiveStatus.toUpperCase()) + (alpacaReachable ? "" : " • ALPACA");
    el.dataset.status = effectiveStatus;
    $("health-summary").textContent = alpacaReachable ? "Alpaca reachable" : "Alpaca unreachable";
    const lines = (health.systemd && health.systemd.stdout_tail) || [];
    $("health-pre").textContent = lines.length ? lines.join("\n") : (health.systemd && health.systemd.error) || "No systemd snapshot lines.";
    const issues = health.log_issues || [];
    $("health-log-issues").textContent = issues.length
      ? issues.map((i) => [i.file, i.level, i.line].filter(Boolean).join(" | ")).join("\n")
      : "No recent warning/error log lines.";
  }

  function renderRiskBudget(riskBudget) {
    const cap = number(riskBudget.portfolio_cap);
    const open = number(riskBudget.open_risk);
    const usedPct = cap > 0 ? Math.min(100, Math.round((open / cap) * 100)) : 0;
    $("portfolio-cap").textContent = compactMoney(riskBudget.portfolio_cap);
    $("portfolio-cap-remaining").textContent = compactMoney(riskBudget.portfolio_cap_remaining);
    $("single-cap").textContent = compactMoney(riskBudget.single_position_cap);
    $("open-position-count").textContent = integer(riskBudget.open_position_count);
    $("risk-budget-pill").textContent = usedPct + "% used";
    $("risk-budget-bar").style.width = usedPct + "%";
    $("risk-budget-bar").className = "ho-progress-bar " + (usedPct >= 90 ? "bg-rose-500" : usedPct >= 65 ? "bg-amber-400" : "bg-emerald-500");
  }

  function renderFunnel(funnel, rejections) {
    $("funnel-source").textContent = funnel.ok ? "latest scan" : "missing";
    $("funnel-candidates").textContent = integer(funnel.candidate_count);
    $("funnel-accepted").textContent = integer(funnel.accepted_count);
    $("funnel-rejected").textContent = integer(funnel.rejected_count);
    $("funnel-research").textContent = integer(funnel.research_candidate_count);
    const summary = (rejections.summary || {});
    const byReason = summary.by_reason || {};
    const rows = Object.entries(byReason).sort((a, b) => b[1] - a[1]).slice(0, 5);
    $("rejection-reasons").innerHTML = rows.map(([reason, count]) =>
      "<div class=\"ho-chip\"><span>" + escapeHtml(labelize(reason)) + "</span><span>" + count + "</span></div>"
    ).join("") || "<div class=\"ho-muted\">No rejection summary available.</div>";
  }

  function renderScanQuality(scan) {
    const data = scan.data || {};
    $("scan-quality-pill").textContent = scan.ok ? "latest scan" : "missing";
    const metrics = [
      ["Symbols", data.symbol_count],
      ["Chain gaps", (data.chain_unavailable_symbols || []).length],
      ["Stale symbols", (data.stale_data_symbols || []).length],
      ["Missing Greeks", (data.missing_greeks_symbols || []).length],
    ];
    $("scan-quality-grid").innerHTML = metrics.map(([label, value]) =>
      "<div><span>" + escapeHtml(label) + "</span><strong>" + integer(value) + "</strong></div>"
    ).join("");
  }

  function renderAiResearch(analytics, activity) {
    const research = analytics.research_trace || {};
    const disagreements = analytics.ai_disagreements || {};
    const researchData = research.data || {};
    const disagreementData = disagreements.data || {};
    $("ai-status").textContent = activity.enabled ? "enabled" : "disabled";
    $("research-trace-count").textContent = integer(researchData.trace_count);
    $("ai-disagreement-count").textContent = integer(disagreementData.summary && disagreementData.summary.total);
    $("ai-spend").textContent = money(activity.daily_spend_usd);
  }

  function renderAttribution(attribution) {
    $("attribution-source").textContent = attribution.ok ? "latest report" : "missing";
    const byStrategy = attribution.data && attribution.data.by_strategy ? attribution.data.by_strategy : {};
    const rows = Object.entries(byStrategy).sort((a, b) => number(b[1].total_pnl) - number(a[1].total_pnl));
    $("attribution-tbody").innerHTML = rows.map(([name, data]) =>
      "<tr>" +
        "<td class=\"text-left\">" + escapeHtml(labelize(name)) + "</td>" +
        "<td class=\"text-right mono\">" + integer(data.trade_count) + "</td>" +
        "<td class=\"text-right mono\">" + rawPct(data.win_rate) + "</td>" +
        "<td class=\"text-right mono\">" + money(data.average_risk_used) + "</td>" +
        "<td class=\"text-right mono " + colorFor(data.return_pct) + "\">" + rawPct(data.return_pct) + "</td>" +
        "<td class=\"text-right mono " + colorFor(data.total_pnl) + "\">" + money(data.total_pnl, true) + "</td>" +
      "</tr>"
    ).join("") || "<tr><td colspan=\"6\" class=\"ho-empty-cell\">No attribution report available</td></tr>";
  }

  function renderIvRanks(rows) {
    $("iv-count").textContent = String(rows.length);
    $("iv-rank-list").innerHTML = rows.slice(0, 8).map((row) => {
      const rank = Math.max(0, Math.min(100, number(row.iv_rank)));
      return "<div class=\"ho-rank-row\">" +
        "<span>" + escapeHtml(row.symbol || "-") + "</span>" +
        "<div class=\"ho-rank-track\"><div class=\"ho-rank-bar\" style=\"width:" + rank + "%\"></div></div>" +
        "<span class=\"mono\">" + rawPct(rank) + "</span>" +
      "</div>";
    }).join("") || "<div class=\"ho-muted\">No IV history available.</div>";
  }

  function renderEarnings(rows) {
    $("earnings-count").textContent = String(rows.length);
    $("earnings-list").innerHTML = rows.slice(0, 8).map((row) =>
      "<div class=\"ho-list-row\">" +
        "<span>" + escapeHtml(row.symbol || "-") + "</span>" +
        "<span>" + escapeHtml(row.earnings_date || "-") + "</span>" +
        "<span class=\"ho-pill\">" + escapeHtml(labelize(row.status || "")) + "</span>" +
      "</div>"
    ).join("") || "<div class=\"ho-muted\">No tracked earnings in the next 14 days.</div>";
  }

  function renderDrift(drift) {
    $("drift-status").textContent = drift.ok ? "available" : "missing";
    const payload = drift.data || {};
    const data = payload.drift || payload;
    const entries = Object.entries(data || {}).slice(0, 3);
    $("drift-summary").textContent = entries.length
      ? entries.map(([key, value]) => labelize(key) + ": " + String(value)).join(" • ")
      : "No drift report available.";
  }

  function labelize(value) {
    return String(value == null ? "" : value).replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
  }

  function setText(id, value) {
    $(id).textContent = value;
  }

  function escapeHtml(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  fetchState();
  setInterval(fetchState, POLL_MS);
  setInterval(updateRefreshTicker, 1000);
})();
