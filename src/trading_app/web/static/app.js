/* Finance Bull dashboard: watchlist charts drawn with TradingView Lightweight Charts v5.
 *
 * The server does the maths (trading_app/services/market.py); this file only
 * draws what /api/market/* returns. Charts load one at a time so a cold cache
 * never sends a burst of requests to Yahoo.
 */
(function () {
  "use strict";
  var LC = window.LightweightCharts;
  var root = document.getElementById("markets");
  if (!root) return;
  if (!LC) { show("The chart library did not load."); return; }

  var store = {
    get: function (k, d) { try { var v = localStorage.getItem("fb." + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem("fb." + k, JSON.stringify(v)); } catch (e) { /* private mode */ } }
  };
  var IND_DEFAULT = { sma: true, bb: true, fib: true, vol: true, rsi: true };
  var state = {
    symbols: [],
    view: store.get("view", "stock") === "sector" ? "sector" : "stock",
    ind: Object.assign({}, IND_DEFAULT, store.get("ind", {})),
    data: {},            // symbol -> chart payload (or {error})
    sectors: null,
    charts: [],          // live chart instances, disposed on re-render
    loading: null
  };
  var PALETTE = ["#2f6db3", "#c08a2b", "#1e8a5a", "#b3322f", "#7a55b5", "#0f8a8a", "#c0612b", "#5d6679",
                 "#9c3d78", "#3b7d23"];

  var grid = document.getElementById("chart-grid");
  var chips = document.getElementById("wl-chips");
  var msg = document.getElementById("wl-msg");
  var form = document.getElementById("wl-form");
  var input = document.getElementById("wl-input");

  // ------------------------------------------------------------------ utils
  function h(tag, attrs) {
    var el = document.createElement(tag);
    if (attrs) Object.keys(attrs).forEach(function (k) {
      if (k === "text") el.textContent = attrs[k];
      else if (k === "cls") el.className = attrs[k];
      else if (k === "style") el.setAttribute("style", attrs[k]);
      else el.setAttribute(k, attrs[k]);
    });
    (function add(list) {
      list.forEach(function (c) {
        if (c == null || c === false) return;
        if (Array.isArray(c)) add(c);
        else el.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
      });
    })(Array.prototype.slice.call(arguments, 2));
    return el;
  }
  function day(t) {          // a chart time (string, business day or timestamp) as YYYY-MM-DD
    if (typeof t === "string") return t;
    if (t && typeof t === "object") return t.year + "-" + String(t.month).padStart(2, "0") + "-" + String(t.day).padStart(2, "0");
    if (typeof t === "number") return new Date(t * 1000).toISOString().slice(0, 10);
    return "";
  }
  function num(v, nd) {
    if (v == null || isNaN(v)) return "—";
    return Number(v).toLocaleString("en-US", { minimumFractionDigits: nd == null ? 2 : nd, maximumFractionDigits: nd == null ? 2 : nd });
  }
  function pct(v, nd, signed) {
    if (v == null || isNaN(v)) return "—";
    var s = Number(v).toFixed(nd == null ? 1 : nd);
    return (signed && v > 0 ? "+" : "") + s + "%";
  }
  function tone(v) { return v == null ? "" : (v >= 0 ? "pos" : "neg"); }
  function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
  function show(text) { if (!msg) return; msg.textContent = text; msg.hidden = !text; }
  function api(path, opts) {
    return fetch(path, Object.assign({ headers: { "Content-Type": "application/json" } }, opts || {}))
      .then(function (r) {
        return r.json().catch(function () { return {}; }).then(function (body) {
          if (!r.ok) throw new Error(body.detail || ("HTTP " + r.status));
          return body;
        });
      });
  }
  function colors() {
    return {
      panel: css("--panel"), ink: css("--ink"), muted: css("--muted"), line: css("--line"), grid: css("--grid"),
      up: css("--up"), down: css("--down"), sma50: css("--c-sma50"), sma200: css("--c-sma200"),
      bb: css("--c-bb"), fib: css("--c-fib"), rsi: css("--c-rsi"), gold: css("--gold")
    };
  }
  function alpha(hex, a) {
    var m = /^#?([0-9a-f]{6})$/i.exec(hex || "");
    if (!m) return hex;
    var n = parseInt(m[1], 16);
    return "rgba(" + (n >> 16 & 255) + "," + (n >> 8 & 255) + "," + (n & 255) + "," + a + ")";
  }

  function baseOptions(c) {
    return {
      autoSize: true,
      layout: {
        background: { type: LC.ColorType.Solid, color: c.panel }, textColor: c.muted, fontSize: 11,
        fontFamily: getComputedStyle(document.body).fontFamily, attributionLogo: true,
        panes: { separatorColor: c.line, separatorHoverColor: c.line, enableResize: false }
      },
      grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
      rightPriceScale: { borderColor: c.line },
      timeScale: { borderColor: c.line, rightOffset: 3, fixLeftEdge: true, fixRightEdge: true },
      crosshair: { mode: LC.CrosshairMode.Normal },
      // Leave the mouse wheel to the page; drag and pinch still pan and zoom.
      handleScroll: { mouseWheel: false, pressedMouseMove: true, horzTouchDrag: true, vertTouchDrag: false },
      handleScale: { mouseWheel: false, pinch: true, axisPressedMouseMove: true, axisDoubleClickReset: true }
    };
  }
  function fit(chart) {
    chart.timeScale().fitContent();
    // Again once layout has settled: autoSize may resize the chart right after creation.
    requestAnimationFrame(function () { try { chart.timeScale().fitContent(); } catch (e) { /* removed */ } });
  }
  function disposeCharts() {
    state.charts.forEach(function (c) { try { c.remove(); } catch (e) { /* already gone */ } });
    state.charts = [];
  }

  // -------------------------------------------------------------- watchlist
  function renderChips() {
    chips.textContent = "";
    if (!state.symbols.length) chips.appendChild(h("span", { cls: "cc-name", text: "No symbols yet: add one." }));
    state.symbols.forEach(function (sym) {
      var b = h("button", { type: "button", title: "Remove " + sym, "aria-label": "Remove " + sym, text: "×" });
      b.addEventListener("click", function () { change({ remove: sym }); });
      chips.appendChild(h("span", { cls: "chip" }, sym, b));
    });
  }
  function change(body) {
    show("");
    return api("/api/market/watchlist", { method: "POST", body: JSON.stringify(body) })
      .then(function (res) {
        state.symbols = res.symbols;
        state.sectors = null;
        renderChips();
        render();
      })
      .catch(function (e) { show(e.message); });
  }
  form.addEventListener("submit", function (ev) {
    ev.preventDefault();
    var sym = input.value.trim();
    if (!sym) return;
    var btn = form.querySelector("button");
    btn.disabled = true; input.disabled = true;
    show("");
    var hint = h("span", { cls: "cc-name", text: "Checking " + sym.toUpperCase() + " at Yahoo…" });
    chips.appendChild(hint);
    change({ add: sym }).then(function () {
      btn.disabled = false; input.disabled = false;
      if (!msg.textContent) { input.value = ""; } else if (hint.parentNode) { hint.remove(); }
      input.focus();
    });
  });
  document.getElementById("wl-reset").addEventListener("click", function () { change({ reset: true }); });

  // --------------------------------------------------------- view switches
  Array.prototype.forEach.call(root.querySelectorAll(".seg button"), function (b) {
    b.addEventListener("click", function () {
      state.view = b.getAttribute("data-view");
      store.set("view", state.view);
      render();
    });
  });
  Array.prototype.forEach.call(root.querySelectorAll("#ind-toggles input"), function (box) {
    var k = box.getAttribute("data-ind");
    box.checked = !!state.ind[k];
    box.addEventListener("change", function () {
      state.ind[k] = box.checked;
      store.set("ind", state.ind);
      render();
    });
  });
  document.addEventListener("themechange", function () { render(); });
  if (window.matchMedia) {
    var mq = matchMedia("(prefers-color-scheme: dark)");
    (mq.addEventListener ? mq.addEventListener.bind(mq, "change") : mq.addListener.bind(mq))(function () { render(); });
  }

  // ---------------------------------------------------------------- render
  function render() {
    Array.prototype.forEach.call(root.querySelectorAll(".seg button"), function (b) {
      b.classList.toggle("on", b.getAttribute("data-view") === state.view);
    });
    document.getElementById("ind-toggles").style.display = state.view === "stock" ? "" : "none";
    disposeCharts();
    grid.textContent = "";
    if (!state.symbols.length) {
      grid.appendChild(h("div", { cls: "empty", style: "grid-column: 1 / -1" },
        "Add a ticker above to chart it — US stocks (AAPL), share classes (BRK.B) or Mexican listings (WALMEX.MX)."));
      return;
    }
    if (state.view === "sector") return renderSectors();
    var token = state.loading = {};
    var cards = state.symbols.map(function (sym) {
      var card = h("article", { cls: "cc", "data-symbol": sym },
        h("div", { cls: "cc-head" }, h("div", null, h("div", { cls: "cc-sym", text: sym }), h("div", { cls: "cc-name", text: "Loading…" }))),
        h("div", { cls: "cc-loading" }));
      grid.appendChild(card);
      return card;
    });
    // One request at a time: polite to Yahoo on a cold cache, instant when cached.
    var i = 0;
    (function next() {
      if (token !== state.loading || i >= cards.length) return;
      var sym = state.symbols[i], card = cards[i];
      i++;
      var cached = state.data[sym];
      var p = cached ? Promise.resolve(cached) : api("/api/market/chart/" + encodeURIComponent(sym))
        .catch(function (e) { return { symbol: sym, error: e.message }; });
      p.then(function (d) {
        if (!d.error) state.data[sym] = d;
        if (token === state.loading) fillStockCard(card, d);
        next();
      });
    })();
  }

  // ------------------------------------------------------------ stock card
  function fillStockCard(card, d) {
    card.textContent = "";
    if (d.error) {
      var rm = h("button", { cls: "btn ghost small", type: "button", text: "Remove " + d.symbol });
      rm.addEventListener("click", function () { change({ remove: d.symbol }); });
      card.appendChild(h("div", { cls: "cc-head" }, h("div", null, h("div", { cls: "cc-sym", text: d.symbol }))));
      card.appendChild(h("div", { cls: "cc-err" }, d.error));
      card.appendChild(h("div", null, rm));
      return;
    }
    var s = d.stats, c = colors();
    var head = h("div", { cls: "cc-head" },
      h("div", null,
        h("div", { cls: "cc-sym" }, d.symbol, d.sector ? h("span", { cls: "badge gold", text: d.sector }) : null),
        h("div", { cls: "cc-name", text: d.name || "" })),
      h("div", { cls: "cc-px" },
        h("div", { cls: "px" }, num(s.last), d.currency ? h("small", { text: d.currency }) : null),
        h("div", { cls: "chg" },
          h("span", { cls: tone(s.change_1d_pct), text: pct(s.change_1d_pct, 2, true) }), " day · ",
          h("span", { cls: tone(s.return_1y_pct), text: pct(s.return_1y_pct, 1, true) }), " 1Y")));
    var ohlc = h("div", { cls: "cc-ohlc" });
    var box = h("div", { cls: "cc-chart" });
    card.appendChild(head);
    card.appendChild(ohlc);
    card.appendChild(box);

    var legend = h("div", { cls: "cc-legend" });
    function key(color, label, dashed) {
      legend.appendChild(h("span", null, h("i", { cls: dashed ? "dash" : "", style: "background:" + color + ";color:" + color }), label));
    }
    var chart = LC.createChart(box, baseOptions(c));
    state.charts.push(chart);
    var candles = chart.addSeries(LC.CandlestickSeries, {
      upColor: c.up, downColor: c.down, borderVisible: false, wickUpColor: c.up, wickDownColor: c.down,
      priceLineColor: c.muted
    }, 0);
    candles.setData(d.bars);

    if (state.ind.bb) {
      var bbOpts = { color: alpha(c.bb, 0.9), lineWidth: 1, lineStyle: LC.LineStyle.Dotted, priceLineVisible: false,
                     lastValueVisible: false, crosshairMarkerVisible: false };
      chart.addSeries(LC.LineSeries, bbOpts, 0).setData(d.bb_upper);
      chart.addSeries(LC.LineSeries, bbOpts, 0).setData(d.bb_lower);
      chart.addSeries(LC.LineSeries, Object.assign({}, bbOpts, { color: alpha(c.bb, 0.5), lineStyle: LC.LineStyle.SparseDotted }), 0)
        .setData(d.bb_mid);
      key(c.bb, "Bollinger 20, 2σ", true);
    }
    if (state.ind.sma) {
      var line = { lineWidth: 2, priceLineVisible: false, lastValueVisible: false, crosshairMarkerVisible: false };
      chart.addSeries(LC.LineSeries, Object.assign({ color: c.sma50 }, line), 0).setData(d.sma50);
      chart.addSeries(LC.LineSeries, Object.assign({ color: c.sma200 }, line), 0).setData(d.sma200);
      key(c.sma50, "SMA 50"); key(c.sma200, "SMA 200");
    }
    if (state.ind.fib && d.fibonacci) {
      var narrow = box.clientWidth < 480;      // on a phone the price labels alone say enough
      d.fibonacci.levels.forEach(function (lv) {
        var edge = lv.ratio === 0 || lv.ratio === 1, golden = Math.abs(lv.ratio - 0.618) < 1e-6;
        candles.createPriceLine({
          price: lv.price, color: alpha(c.fib, edge ? 0.55 : 0.9), lineWidth: golden ? 2 : 1,
          lineStyle: edge ? LC.LineStyle.Solid : LC.LineStyle.Dashed, axisLabelVisible: true,
          title: narrow ? "" : (lv.ratio * 100).toFixed(1) + "%"
        });
      });
      key(c.fib, "Fibonacci " + (d.fibonacci.direction === "up" ? "(advance, from the high)" : "(decline, from the low)"), true);
    }
    if (state.ind.vol) {
      var vol = chart.addSeries(LC.HistogramSeries, {
        priceFormat: { type: "volume" }, priceScaleId: "vol", priceLineVisible: false, lastValueVisible: false
      }, 0);
      vol.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
      vol.setData(d.volume.map(function (v) { return { time: v.time, value: v.value, color: alpha(v.up ? c.up : c.down, 0.35) }; }));
    }
    candles.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: state.ind.vol ? 0.2 : 0.06 } });
    if (state.ind.rsi) {
      var rsi = chart.addSeries(LC.LineSeries, {
        color: c.rsi, lineWidth: 2, priceLineVisible: false, lastValueVisible: true,
        autoscaleInfoProvider: function () { return { priceRange: { minValue: 0, maxValue: 100 } }; }
      }, 1);
      rsi.setData(d.rsi);
      [[70, c.down], [50, c.muted], [30, c.up]].forEach(function (lv) {
        rsi.createPriceLine({ price: lv[0], color: alpha(lv[1], 0.6), lineWidth: 1, lineStyle: LC.LineStyle.Dashed,
                              axisLabelVisible: false, title: lv[0] === 50 ? "" : String(lv[0]) });
      });
      var panes = chart.panes();
      if (panes.length > 1) { panes[0].setStretchFactor(2.5); panes[1].setStretchFactor(1); }
      key(c.rsi, "RSI 14 (70 / 30)");
    }
    fit(chart);

    // OHLC readout that follows the crosshair, defaulting to the last bar.
    var last = d.bars[d.bars.length - 1];
    function readout(bar, t) {
      if (!bar) return;
      ohlc.textContent = (t || last.time) + "  O " + num(bar.open) + "  H " + num(bar.high) + "  L " + num(bar.low) + "  C " + num(bar.close);
    }
    readout(last);
    chart.subscribeCrosshairMove(function (param) {
      var bar = param && param.seriesData ? param.seriesData.get(candles) : null;
      if (bar && param.time) readout(bar, day(param.time));
      else readout(last);
    });

    card.appendChild(legend);
    card.appendChild(statsBlock(d));
  }

  function statsBlock(d) {
    var s = d.stats, f = d.fibonacci || {};
    var kv = h("dl", { cls: "kv" });
    function item(label, value, sub, cls) {
      kv.appendChild(h("div", null, h("dt", { text: label }),
        h("dd", { cls: cls || "" }, value, sub ? h("small", { text: sub }) : null)));
    }
    var span = (s.high_52w - s.low_52w) || 1;
    var pos = Math.max(0, Math.min(100, (s.last - s.low_52w) / span * 100));
    var range = h("div", { cls: "range" }, h("b", { style: "left: calc(" + pos.toFixed(1) + "% - 1px)" }));
    kv.appendChild(h("div", null, h("dt", { text: "52-week range" }),
      h("dd", null, num(s.low_52w) + " – " + num(s.high_52w), range, h("small", { text: pct(s.from_high_pct, 1, true) + " from the high" }))));
    item("Volatility", pct(s.volatility_pct), "annualized, 1Y");
    item("Max drawdown", pct(s.max_drawdown_pct), "peak to trough, 1Y", "neg");
    var r = s.rsi14;
    item("RSI 14", num(r, 1), r == null ? "" : (r >= 70 ? "overbought zone" : r <= 30 ? "oversold zone" : "neutral"));
    var trend = { up: "Uptrend", down: "Downtrend", mixed: "Mixed" }[s.trend] || "—";
    var cross = s.sma50 != null && s.sma200 != null ? (s.sma50 > s.sma200 ? "SMA 50 above 200 (golden cross)" : "SMA 50 below 200 (death cross)") : "";
    item("Trend", trend, cross, s.trend === "up" ? "pos" : s.trend === "down" ? "neg" : "");
    item("vs SMA 200", pct(s.vs_sma200_pct, 1, true), "distance from the 200-day average", tone(s.vs_sma200_pct));
    item("Fibonacci zone", f.zone || "—", f.direction === "up" ? "retracement of the advance" : "retracement of the decline");
    item("ATR 14", pct(s.atr14_pct, 2), "average daily range");
    return kv;
  }

  // ----------------------------------------------------------- sector view
  function renderSectors() {
    var token = state.loading = {};
    var wait = h("div", { cls: "cc", style: "grid-column: 1 / -1" }, h("div", { cls: "cc-name", text: "Grouping the watchlist by sector…" }),
                 h("div", { cls: "cc-loading" }));
    grid.appendChild(wait);
    var p = state.sectors ? Promise.resolve(state.sectors) : api("/api/market/sectors");
    p.then(function (res) {
      if (token !== state.loading) return;
      state.sectors = res;
      grid.textContent = "";
      var errs = Object.keys(res.errors || {});
      if (errs.length) show("Not charted: " + errs.map(function (k) { return k + " (" + res.errors[k] + ")"; }).join("; "));
      if (!res.sectors.length) { grid.appendChild(h("div", { cls: "empty", style: "grid-column: 1 / -1", text: "Nothing to group yet." })); return; }
      res.sectors.forEach(function (sec) { sectorCard(sec, res, grid); });
    }).catch(function (e) {
      if (token !== state.loading) return;
      grid.textContent = "";
      show(e.message);
    });
  }

  function sectorCard(sec, res, parent) {
    var c = colors(), st = sec.stats;
    var card = h("article", { cls: "cc" });
    parent.appendChild(card);            // in the page first, so the chart is created at its real width
    card.appendChild(h("div", { cls: "cc-head" },
      h("div", null,
        h("div", { cls: "cc-sym" }, sec.sector),
        h("div", { cls: "cc-name", text: st.members + (st.members === 1 ? " stock" : " stocks") + " · equal-weight index, rebased to 0% a year ago" })),
      h("div", { cls: "cc-px" },
        h("div", { cls: "px " + tone(st.return_1y_pct), text: pct(st.return_1y_pct, 1, true) }),
        h("div", { cls: "chg" }, st.vs_benchmark_pct == null ? "1Y" :
          h("span", { cls: tone(st.vs_benchmark_pct), text: (st.vs_benchmark_pct >= 0 ? "+" : "") + st.vs_benchmark_pct.toFixed(1) + " pts vs S&P 500" })))));
    var box = h("div", { cls: "cc-chart sector" });
    card.appendChild(box);
    var chart = LC.createChart(box, Object.assign(baseOptions(c), {
      localization: { priceFormatter: function (v) { return (v > 0 ? "+" : "") + v.toFixed(1) + "%"; } }
    }));
    state.charts.push(chart);
    var legend = h("div", { cls: "cc-legend" });
    function key(color, label, dashed) {
      legend.appendChild(h("span", null, h("i", { cls: dashed ? "dash" : "", style: "background:" + color + ";color:" + color }), label));
    }
    var symbols = Object.keys(sec.series);
    symbols.forEach(function (sym, i) {
      var col = PALETTE[i % PALETTE.length];
      chart.addSeries(LC.LineSeries, { color: alpha(col, 0.85), lineWidth: 1, priceLineVisible: false, lastValueVisible: false,
                                       crosshairMarkerVisible: false }).setData(sec.series[sym]);
      key(col, sym + " " + pct((sec.members[sym] || {}).return_1y_pct, 1, true));
    });
    if (sec.benchmark) {
      chart.addSeries(LC.LineSeries, { color: c.muted, lineWidth: 2, lineStyle: LC.LineStyle.Dashed, priceLineVisible: false,
                                       lastValueVisible: true, title: "S&P 500" }).setData(sec.benchmark);
      key(c.muted, "S&P 500 " + pct(st.benchmark_1y_pct, 1, true), true);
    }
    var idx = chart.addSeries(LC.LineSeries, { color: c.ink, lineWidth: 3, priceLineVisible: false, lastValueVisible: true,
                                               title: "Sector" });
    idx.setData(sec.index);
    idx.createPriceLine({ price: 0, color: alpha(c.muted, 0.7), lineWidth: 1, lineStyle: LC.LineStyle.Solid, axisLabelVisible: false });
    key(c.ink, "Sector index");
    if (sec.index_sma50 && sec.index_sma50.length) {
      chart.addSeries(LC.LineSeries, { color: c.sma50, lineWidth: 1, lineStyle: LC.LineStyle.Dotted, priceLineVisible: false,
                                       lastValueVisible: false, crosshairMarkerVisible: false }).setData(sec.index_sma50);
      key(c.sma50, "Index SMA 50", true);
    }
    fit(chart);
    card.appendChild(legend);

    var kv = h("dl", { cls: "kv" });
    [["1Y return", pct(st.return_1y_pct, 1, true), tone(st.return_1y_pct)],
     ["vs S&P 500", st.vs_benchmark_pct == null ? "—" : (st.vs_benchmark_pct >= 0 ? "+" : "") + st.vs_benchmark_pct.toFixed(1) + " pts", tone(st.vs_benchmark_pct)],
     ["Volatility", pct(st.volatility_pct), ""],
     ["Max drawdown", pct(st.max_drawdown_pct), "neg"]].forEach(function (r) {
      kv.appendChild(h("div", null, h("dt", { text: r[0] }), h("dd", { cls: r[2], text: r[1] })));
    });
    card.appendChild(kv);

    var rows = symbols.map(function (sym) {
      var m = sec.members[sym] || {};
      return h("tr", null,
        h("td", null, h("strong", { text: sym }), h("div", { cls: "cc-name", text: (res.names || {})[sym] || "" })),
        h("td", { cls: "num " + tone(m.return_1y_pct), text: pct(m.return_1y_pct, 1, true) }),
        h("td", { cls: "num " + tone(m.vs_benchmark_pct), text: m.vs_benchmark_pct == null ? "—" : (m.vs_benchmark_pct >= 0 ? "+" : "") + m.vs_benchmark_pct.toFixed(1) }),
        h("td", { cls: "num", text: pct(m.volatility_pct) }),
        h("td", { cls: "num", text: num(m.rsi14, 0) }),
        h("td", { cls: m.trend === "up" ? "pos" : m.trend === "down" ? "neg" : "", text: { up: "Up", down: "Down", mixed: "Mixed" }[m.trend] || "—" }));
    });
    card.appendChild(h("div", { cls: "table-wrap members" }, h("table", null,
      h("tr", null, h("th", { text: "Stock" }), h("th", { cls: "num", text: "1Y" }), h("th", { cls: "num", text: "vs S&P (pts)" }),
        h("th", { cls: "num", text: "Vol" }), h("th", { cls: "num", text: "RSI" }), h("th", { text: "Trend" })),
      rows)));
    return card;
  }

  // ------------------------------------------------------------------ start
  api("/api/market/watchlist").then(function (res) {
    state.symbols = res.symbols;
    renderChips();
    render();
  }).catch(function (e) { show(e.message); });
})();
