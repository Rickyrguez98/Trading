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

  // ------------------------------------------------- next month (model)
  function outlookCell(td, ol, lot) {
    td.textContent = "";
    function line(cls) { var d = document.createElement("div"); if (cls) d.className = cls; td.appendChild(d); return d; }
    function add(parent, text, cls, tag) {
      var e = document.createElement(tag || "span"); if (cls) e.className = cls; e.textContent = text; parent.appendChild(e); return e;
    }
    if (!ol || ol.status === "error") { add(line("cc-name"), "no prices: " + ((ol && ol.error) || "unavailable")); return; }
    if (ol.status !== "ok") { add(line("cc-name"), "not enough price history (" + (ol.history_days || 0) + " days)"); return; }
    var range = line("ol-range");
    add(range, "$" + price(ol.expected_low) + " – $" + price(ol.expected_high));
    var sell = line("ol-sell");
    add(sell, "Sell limit ");
    add(sell, "$" + price(ol.sell_limit), "", "b");
    if (lot && lot.limit_vs_cost_pct != null) add(sell, " " + pct(lot.limit_vs_cost_pct), toneOf(lot.limit_vs_cost_pct));
    var stop = line("ol-sell");
    add(stop, "Stop ");
    add(stop, "$" + price(ol.stop), "", "b");
    if (lot && lot.stop_vs_cost_pct != null) add(stop, " " + pct(lot.stop_vs_cost_pct), toneOf(lot.stop_vs_cost_pct));
    var note = line("cc-name");
    var held = ol.validated_months ? "range held " + ol.range_held_months + " of " + ol.validated_months + " past months"
                                   : "short history (" + ol.history_days + " days): not back-tested";
    note.textContent = "fills ~" + Math.round(ol.sell_limit_touch * 100) + "% / hit ~" + Math.round(ol.stop_touch * 100) +
      "% of months · " + held;
    if (lot && lot.limit_above_cost === false) add(line("cc-name neg"), "limit is below what this lot cost");
  }

  function loadOutlook() {
    var cells = table.querySelectorAll("td.js-outlook");
    if (!cells.length) return;
    fetch("/api/portfolio/outlook").then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    }).then(function (res) {
      Array.prototype.forEach.call(table.querySelectorAll("tr[data-open]"), function (tr) {
        var key = tr.getAttribute("data-open"), sym = key.split("|")[0];
        var td = tr.querySelector("td.js-outlook");
        if (td) outlookCell(td, res.symbols[sym], res.lots[key]);
      });
    }).catch(function (e) {
      Array.prototype.forEach.call(cells, function (td) { td.textContent = "unavailable (" + e.message + ")"; });
    });
  }

  setInterval(function () { if (nextAt && !busy && !failed) label(); }, 1000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && nextAt && Date.now() >= nextAt) refresh();     // a tab left in the background catches up
  });
  refresh();
  loadOutlook();          // daily numbers: once per page load is enough
})();
