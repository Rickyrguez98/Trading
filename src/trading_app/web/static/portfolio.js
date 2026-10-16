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

  setInterval(function () { if (nextAt && !busy && !failed) label(); }, 1000);
  document.addEventListener("visibilitychange", function () {
    if (!document.hidden && nextAt && Date.now() >= nextAt) refresh();     // a tab left in the background catches up
  });
  refresh();
})();
