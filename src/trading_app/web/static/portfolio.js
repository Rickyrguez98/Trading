/* Portfolio page: live prices for open positions, refreshed every five minutes.
 *
 * The table and totals are rendered by the server from cached prices; this
 * asks /api/portfolio/quotes (which goes to Yahoo at most every five minutes)
 * and fills in each open lot's current price, value and result if sold now.
 */
(function () {
  "use strict";
  var table = document.getElementById("ops");
  var status = document.getElementById("quote-status");
  if (!table || !status) return;
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
    Array.prototype.forEach.call(table.querySelectorAll("tr[data-open]"), function (tr) {
      var o = byKey[tr.getAttribute("data-open")];
      if (!o) return;
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
      el("kpi-total").textContent = usd(s.total_pl, true);
      setTone(el("kpi-total"), s.total_pl);
      var tv = el("tot-value"), tp = el("tot-pl");
      if (tv) tv.firstChild.textContent = usd(s.market_value);
      if (tp) { tp.firstChild.textContent = usd(s.total_pl, true); setTone(tp.firstChild, s.total_pl); }
    }
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
             : x.tests ? "too few tests" : "not testable";
    td.appendChild(el2("small", note));
    td.title = "Share of simulated futures ending above today's price. Track record: " + note + ".";
  }

  function recCell(td, plan, ol) {
    td.textContent = "";
    if (!plan) {
      td.appendChild(el2("small", ol && ol.status === "insufficient_history" ? "not enough price history" : "no outlook"));
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
      l.appendChild(document.createTextNode(" vs cost · ~" + Math.round(plan.p_fill * 100) + "% chance in a month"));
      td.appendChild(l);
      var st = document.createElement("div");
      st.appendChild(document.createTextNode("Stop " + money(plan.stop) + " "));
      st.appendChild(el2("span", pct(plan.stop_vs_cost_pct), toneOf(plan.stop_vs_cost_pct)));
      td.appendChild(st);
    }
    td.appendChild(el2("small", "today " + pct(plan.now_vs_cost_pct) + " vs cost · " + plan.confidence));
    td.title = plan.why;
  }

  function loadOutlook() {
    var rows = table.querySelectorAll("tr[data-open]");
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
      });
    }).catch(function (e) {
      Array.prototype.forEach.call(table.querySelectorAll("td.fc, td.js-rec"), function (td) {
        td.textContent = "unavailable (" + e.message + ")";
      });
    });
  }

  setInterval(function () { if (nextAt && !busy && !failed) label(); }, 1000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && nextAt && Date.now() >= nextAt) refresh();     // a tab left in the background catches up
  });
  refresh();
  loadOutlook();          // daily numbers: once per page load is enough
})();
