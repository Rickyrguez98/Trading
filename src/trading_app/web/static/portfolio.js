/* Portfolio page: live prices for open positions, refreshed every five minutes.
 *
 * The table and totals are rendered by the server from cached prices; this
 * asks /api/portfolio/quotes (which goes to Yahoo at most every five minutes)
 * and fills in each open lot's current price, value and result if sold now.
 */
(function () {
  "use strict";
  var status = document.getElementById("quote-status");
  var wraps = Array.prototype.slice.call(document.querySelectorAll(".table-wrap.ops[data-kind]"));
  if (!wraps.length || !status) return;
  function openRows() { return document.querySelectorAll("tr[data-open]"); }
  var REFRESH = 300, nextAt = 0, asOf = null, timer = null, busy = false, failed = false;

  function usd(v, signed) {
    if (v == null || isNaN(v)) return "—";
    var sign = signed ? (v > 0 ? "+" : v < 0 ? "−" : "") : (v < 0 ? "−" : "");
    return sign + "$" + Math.abs(v).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  }
  function price(v) {
    if (v == null || isNaN(v)) return "…";
    var nd = Math.abs(v) < 10 ? 4 : 2;
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: nd, maximumFractionDigits: nd });
  }
  function pct(v) { return v == null || isNaN(v) ? "—" : (v > 0 ? "+" : "") + v.toFixed(2) + "%"; }
  function toneOf(v) { return v == null ? "" : v >= 0 ? "pos" : "neg"; }
  function setTone(el, v) { el.classList.remove("pos", "neg"); var t = toneOf(v); if (t) el.classList.add(t); }
  function el(id) { return document.getElementById(id); }

  function apply(res) {
    var byKey = {};
    (res.operations || []).forEach(function (o) { byKey[o.symbol + "|" + o.buy_order] = o; });
    Array.prototype.forEach.call(openRows(), function (tr) {
      var o = byKey[tr.getAttribute("data-open")];
      if (!o) return;
      tr.setAttribute("data-value", o.market_value == null ? "" : o.market_value);
      tr.querySelector(".js-price").textContent = price(o.current_price);
      tr.querySelector(".js-value").textContent = o.market_value == null ? "…" : usd(o.market_value);
      var pl = tr.querySelector(".js-pl"), ret = tr.querySelector(".js-ret");
      pl.textContent = o.pl == null ? "…" : usd(o.pl, true);
      ret.textContent = pct(o.return_pct);
      setTone(pl, o.pl); setTone(ret, o.pl);
      if (o.price_as_of && (!asOf || o.price_as_of > asOf)) asOf = o.price_as_of;
    });
    var s = res.summary || {};
    if (s.market_value != null) {
      el("kpi-value").textContent = usd(s.market_value);
      el("kpi-unreal").innerHTML = "";
      var span = document.createElement("span");
      span.className = toneOf(s.unrealized_pl);
      span.textContent = usd(s.unrealized_pl, true) + " · " + pct(s.unrealized_pct);
      el("kpi-unreal").appendChild(span);
      el("kpi-unreal").appendChild(document.createTextNode(" if sold now" + (s.unpriced ? " (" + s.unpriced + " lot(s) unpriced)" : "")));
      if (s.stale_prices) el("kpi-unreal").appendChild(document.createTextNode(" · " + s.stale_prices + " last-known price(s)"));
      el("kpi-total").textContent = usd(s.total_pl, true);
      setTone(el("kpi-total"), s.total_pl);
    }
    subtotals();
  }

  // Each table's subtotal: what its lots cost, are worth now and would gain or lose if sold now.
  function subtotals() {
    wraps.forEach(function (w) {
      var cost = 0, value = 0, priced = true;
      Array.prototype.forEach.call(w.querySelectorAll("tr[data-open]"), function (tr) {
        cost += parseFloat(tr.getAttribute("data-cost")) || 0;
        var v = parseFloat(tr.getAttribute("data-value"));
        if (isNaN(v)) priced = false; else value += v;
      });
      var c = w.querySelector(".tf-cost"), v = w.querySelector(".tf-value"),
          p = w.querySelector(".tf-pl"), r = w.querySelector(".tf-ret");
      if (c) c.textContent = usd(cost);
      if (!priced) return;
      if (v) v.textContent = usd(value);
      if (p) { p.textContent = usd(value - cost, true); setTone(p, value - cost); }
      if (r && cost) { r.textContent = pct((value / cost - 1) * 100); setTone(r, value - cost); }
    });
  }

  function label() {
    var left = Math.max(0, Math.round((nextAt - Date.now()) / 1000));
    var when = asOf ? new Date(asOf).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric",
                                                                hour: "numeric", minute: "2-digit" }) + " New York time" : "—";
    status.textContent = "Open positions priced as of " + when + " (latest regular-session trade, Yahoo Finance) · next update in " +
      Math.floor(left / 60) + ":" + String(left % 60).padStart(2, "0");
  }

  function refresh() {
    busy = true;
    status.textContent = "Updating prices…";
    fetch("/api/portfolio/quotes").then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (res) {
      REFRESH = res.refresh_seconds || REFRESH;
      apply(res);
      failed = false;
    }).catch(function (e) {
      failed = true;
      status.textContent = "Could not update prices (" + e.message + "); trying again in five minutes.";
    }).then(function () {
      busy = false;
      nextAt = Date.now() + REFRESH * 1000;
      if (!failed) label();
      clearTimeout(timer);
      timer = setTimeout(refresh, REFRESH * 1000);
    });
  }

  // ------------------------------------------- forecasts and recommendation
  function el2(tag, text, cls) { var e = document.createElement(tag); if (cls) e.className = cls; e.textContent = text; return e; }
  function money(v) { return "$" + price(v); }

  function trendCell(td, x) {
    td.textContent = "";
    var arrow = { up: "▲ Up", down: "▼ Down", flat: "→ Flat" }[x.trend] || "—";
    var t = el2("span", arrow + " " + Math.round(x.p_up * 100) + "%",
                "trend " + (x.trend === "up" ? "pos" : x.trend === "down" ? "neg" : "flat"));
    td.appendChild(t);
    var note = x.validated ? "✓ right " + x.hits + "/" + x.tests
             : x.testable ? "right " + x.hits + "/" + x.tests + ", unproven"
             : "needs " + Math.round(x.years_needed) + " yrs of prices (has " + Math.round(x.years_available) + ")";
    td.appendChild(el2("small", note));
    td.title = "Share of simulated futures ending above today's price. Track record: " + note + ".";
  }

  function ruleRecord(name, r) {
    if (!r || r.status !== "ok") return null;
    var label = name === "range" ? "Range trading" : "Trend following";
    if (r.verdict === "too_little_history") return label + ": too little history to judge";
    var two = r.past_2y_rule_pct != null
      ? " · 2 yrs: " + pct(r.past_2y_rule_pct) + " vs holding " + pct(r.past_2y_hold_pct) + " (" + r.past_2y_trades + " trades)"
      : "";
    return label + (r.verdict === "did_not_help" ? " lost to holding" : " beat holding") +
      ": won " + r.past_wins + "/" + r.past_months + " months" + two;
  }

  function recCell(td, plan, ol) {
    td.textContent = "";
    if (!plan) {
      td.appendChild(el2("small", ol && ol.status === "insufficient_history" ? "not enough price history" : "no outlook"));
      return;
    }
    var rules = plan.rules || {};
    if (plan.plan === "range") {
      var sw = rules.range;
      td.appendChild(el2("b", "Trade the range"));
      var a1 = document.createElement("div");
      a1.appendChild(document.createTextNode("Sell at " + money(sw.sell_at) + " "));
      a1.appendChild(el2("span", pct(plan.sell_vs_cost_pct), toneOf(plan.sell_vs_cost_pct)));
      td.appendChild(a1);
      td.appendChild(el2("div", "Buy back at " + money(sw.buy_back_at) + " · bands ±" + sw.band_pct.toFixed(1) +
                                "%, moved weekly"));
    } else if (plan.plan === "trend") {
      var tr = rules.trend;
      td.appendChild(el2("b", "Follow the trend"));
      td.appendChild(el2("div", tr.above_average
        ? "Hold while it closes above " + money(tr.average) + " (20-day average); sell if it closes below"
        : "It is below its 20-day average (" + money(tr.average) + "): the rule says be out; buy back if it closes above"));
    }
    if (plan.plan === "range" || plan.plan === "trend") {
      var rec = ruleRecord(plan.plan, rules[plan.plan]);
      td.appendChild(el2("small", rec + ((rules[plan.plan] || {}).verdict === "proven" ? " · ✓ proven" : " · not proven")));
      td.appendChild(el2("small", "chosen on the same two years it is judged on; more trades also mean more commissions and taxes"));
      td.title = "A multi-trade rule that beat holding this stock over its real last two years.";
      return;
    }
    if (plan.plan === "sell_now") {
      td.appendChild(el2("b", "Sell now ≈ " + money(plan.price)));
      var a = document.createElement("div");
      a.appendChild(el2("span", pct(plan.now_vs_cost_pct), toneOf(plan.now_vs_cost_pct)));
      a.appendChild(document.createTextNode(" vs your cost"));
      td.appendChild(a);
    } else {
      td.appendChild(el2("b", "Limit sell " + money(plan.limit)));
      var l = document.createElement("div");
      l.appendChild(el2("span", pct(plan.exit_vs_cost_pct), toneOf(plan.exit_vs_cost_pct)));
      l.appendChild(document.createTextNode(" vs cost · ~" + Math.round(plan.p_fill * 100) + "% chance within " +
                                            (plan.horizon_label || "a month")));
      td.appendChild(l);
      var st = document.createElement("div");
      st.appendChild(document.createTextNode("Stop " + money(plan.stop) + " "));
      st.appendChild(el2("span", pct(plan.stop_vs_cost_pct), toneOf(plan.stop_vs_cost_pct)));
      td.appendChild(st);
    }
    if (plan.style === "short") {
      ["range", "trend"].forEach(function (name) {
        var n = ruleRecord(name, rules[name]);
        if (n) td.appendChild(el2("small", n));
      });
    }
    td.appendChild(el2("small", "today " + pct(plan.now_vs_cost_pct) + " vs cost · " + plan.confidence));
    td.title = plan.why;
  }

  function fmtDate(iso) {
    if (!iso) return "";
    var d = new Date(iso + "T12:00:00");
    return d.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
  }

  function prognosisCell(td, pg, ol) {
    td.textContent = "";
    if (!pg || !pg.verdict) {
      td.appendChild(el2("small", ol && ol.status === "insufficient_history" ? "not enough price history" : "no outlook"));
      return;
    }
    var tone = pg.verdict === "wait" ? "pos" : pg.verdict === "fall" ? "neg" : "flat";
    td.appendChild(el2("span", pg.headline, "verdict trend " + tone));
    if (pg.lean && pg.lean !== "flat") td.appendChild(el2("small", "the unproven model leans " + pg.lean +
                                                       (ol && ol.style === "short" ? " over a month" : " over a year")));
    var e = document.createElement("div");
    e.appendChild(document.createTextNode("Expected " + money(pg.expected) + " by " + fmtDate(pg.expected_date) + " "));
    e.appendChild(el2("span", pct(pg.expected_vs_cost_pct), toneOf(pg.expected_vs_cost_pct)));
    td.appendChild(e);
    var b = document.createElement("div");
    if (pg.in_profit) {
      b.textContent = "Above your break-even (" + money(pg.break_even) + ")";
    } else {
      b.textContent = "Break-even " + money(pg.break_even) + ": " +
        (pg.break_even_date ? "by " + fmtDate(pg.break_even_date) + " (middle scenario)" : "not within a year (middle scenario)");
    }
    td.appendChild(b);
    if (pg.p_halve_1y != null) td.appendChild(el2("small", "Chance of halving within a year: " + Math.round(pg.p_halve_1y * 100) + "%"));
    td.appendChild(el2("small", pg.basis));
  }

  // Freeze the main columns (desktop) so only the forecasts scroll sideways, in every table.
  function freeze() {
    wraps.forEach(function (wrap) {
      var wide = wrap.clientWidth >= 900;
      wrap.classList.toggle("frozen", wide);
      var lefts = [], x = 0;
      Array.prototype.forEach.call(wrap.querySelectorAll("thead th.fz"), function (th) {
        lefts[+th.getAttribute("data-fz")] = x; x += th.offsetWidth;
      });
      Array.prototype.forEach.call(wrap.querySelectorAll(".fz"), function (cell) {
        cell.style.left = wide ? lefts[+cell.getAttribute("data-fz")] + "px" : "";
      });
    });
  }
  window.addEventListener("resize", function () { clearTimeout(freeze.t); freeze.t = setTimeout(freeze, 120); });
  freeze();

  function loadOutlook() {
    var rows = openRows();
    if (!rows.length) return;
    fetch("/api/portfolio/outlook").then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (res) {
      Array.prototype.forEach.call(rows, function (tr) {
        var key = tr.getAttribute("data-open"), sym = key.split("|")[0], ol = res.symbols[sym];
        Array.prototype.forEach.call(tr.querySelectorAll("td.fc"), function (td) {
          var x = ol && ol.status === "ok" ? ol.horizons[td.getAttribute("data-h")] : null;
          var k = td.getAttribute("data-k");
          if (!x) { td.textContent = k === "trend" && ol && ol.status === "insufficient_history" ? "short history" : "—"; return; }
          if (k === "trend") trendCell(td, x); else td.textContent = money(x[k]);
        });
        var rec = tr.querySelector("td.js-rec");
        if (rec) recCell(rec, res.plans[key], ol);
        var pgc = tr.querySelector("td.js-pg");
        if (pgc) prognosisCell(pgc, (res.prognoses || {})[key], ol);
      });
      freeze();                      // cell contents changed: recompute the frozen offsets
    }).catch(function (e) {
      Array.prototype.forEach.call(document.querySelectorAll("td.fc, td.js-rec, td.js-pg"), function (td) {
        td.textContent = "unavailable (" + e.message + ")";
      });
    });
  }

  setInterval(function () { if (nextAt && !busy && !failed) label(); }, 1000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && nextAt && Date.now() >= nextAt) refresh();     // a tab left in the background catches up
  });
  subtotals();
  refresh();
  loadOutlook();          // daily numbers: once per page load is enough
})();
