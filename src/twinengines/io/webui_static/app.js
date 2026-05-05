async function loadAll() {
  try {
    // === Current window ===
    var cw = await fetch('/api/current_window').then(r => r.json());
    if (cw.ok) {
      var wts = 0;
      try { wts = parseInt((cw.window_id || 'w0').replace('w','')); } catch(e) {}
      var d = new Date(wts);
      var ts = d.getUTCHours().toString().padStart(2,'0') + ':' + d.getUTCMinutes().toString().padStart(2,'0');
      setText('cw-wid', ts);
      setText('cw-seq', cw.prefix || '-');
      setText('cw-T', cw.T != null ? Math.round(cw.T) + 's' : '-');
      setText('cw-p', cw.p_adj != null ? cw.p_adj.toFixed(3) : '-');
      setText('cw-pl', cw.p_lower != null ? cw.p_lower.toFixed(3) : '-');
      setText('cw-d', cw.d_abs != null ? cw.d_abs.toFixed(4) : '-');
      setText('cw-au', cw.ask_up != null ? cw.ask_up.toFixed(3) : '-');
      setText('cw-ad', cw.ask_down != null ? cw.ask_down.toFixed(3) : '-');
      setText('cw-evr', cw.ev_rev != null ? cw.ev_rev.toFixed(4) : '-');
      setText('cw-evt', cw.ev_trend != null ? cw.ev_trend.toFixed(4) : '-');
      setText('cw-dir', cw.best_dir ? cw.best_dir.toUpperCase() : '-');

      if (cw.status === 'FILLED') {
        setText('cw-status', '已下单 ' + (cw.best_dir || '').toUpperCase());
        setClass('cw-status', 'signal-ok');
      } else if (cw.status && cw.status.indexOf('R:d>') === 0) {
        setText('cw-status', '反转被拒 d=' + (cw.d_abs||0).toFixed(4) + '>' + (cw.d_cliff||0).toFixed(4));
        setClass('cw-status', 'signal-no');
      } else if (cw.status && cw.status.indexOf('R:p<') === 0) {
        setText('cw-status', '反转被拒 p=' + (cw.p_lower||0).toFixed(3) + '<' + (cw.p_min_r||0).toFixed(3));
        setClass('cw-status', 'signal-no');
      } else if (cw.status && cw.status.indexOf('EV<') === 0) {
        setText('cw-status', cw.status);
        setClass('cw-status', 'signal-no');
      } else if (cw.status === 'Kelly<2.5') {
        setText('cw-status', '金额不足');
        setClass('cw-status', 'signal-no');
      } else if (cw.status === 'T<5s') {
        setText('cw-status', '窗口结束');
        setClass('cw-status', 'signal-wait');
      } else if (cw.status === 'EV neg') {
        setText('cw-status', 'EV负');
        setClass('cw-status', 'signal-no');
      } else if (cw.window_id && cw.status) {
        setText('cw-status', cw.status);
        setClass('cw-status', 'signal-no');
      } else {
        setText('cw-status', '等待窗口...');
        setClass('cw-status', 'signal-wait');
      }
    }

    // === Current window fill info ===
    var fillInfo = document.getElementById('fill-info');
    var fillSt = cw.status || '';
    var fillDir = cw.best_dir || '';
    var td = cw.td || '';
    var dAbs = cw.d_abs != null ? cw.d_abs.toFixed(4) : '-';
    var dCliff = cw.d_cliff != null ? cw.d_cliff.toFixed(4) : '-';
    var pLow = cw.p_lower != null ? cw.p_lower.toFixed(3) : '-';
    var pMin = cw.p_min_r != null ? cw.p_min_r.toFixed(3) : '-';
    var evR = cw.ev_rev != null ? cw.ev_rev.toFixed(4) : '-';
    var evT = cw.ev_trend != null ? cw.ev_trend.toFixed(4) : '-';
    var revDok = cw.r_d_ok ? 'dOK' : 'dNO';
    var revPok = cw.r_p_ok ? 'pOK' : 'pNO';

    // Compute trend OK (no d/p filter, just EV > 0)
    var trendOk = (evT !== '-' && parseFloat(evT) > 0) ? 'EV可' : 'EV不可';
    var reversalOk = (cw.r_d_ok && cw.r_p_ok) ? 'd+p可' : (cw.r_d_ok ? 'd可p否' : (cw.r_p_ok ? 'd否p可' : 'd+p否'));

    if (fillSt === 'FILLED' || fillSt === 'filled') {
      var fa = cw.fill_amt != null ? '$' + cw.fill_amt.toFixed(2) : '-';
      var fask = cw.fill_ask != null ? cw.fill_ask.toFixed(4) : '-';
      var fev = cw.fill_ev != null ? cw.fill_ev.toFixed(4) : '-';
      fillInfo.innerHTML = '已成交: <strong class="pos">' + fillDir.toUpperCase()
        + '</strong> | 金额=' + fa + ' | 买入价=' + fask + ' | EV=' + fev
        + ' | 反转=' + reversalOk + ' 顺势=' + trendOk;
    } else if (fillSt && fillSt.indexOf('R:d>') === 0) {
      fillInfo.innerHTML = '反转被拒(d): d=' + dAbs + ' > d_cliff=' + dCliff
        + ' | 顺势=' + trendOk;
    } else if (fillSt && fillSt.indexOf('R:p<') === 0) {
      fillInfo.innerHTML = '反转被拒(p): p_lower=' + pLow + ' < p_min=' + pMin
        + ' | 顺势=' + trendOk;
    } else if (fillSt === 'EV neg') {
      fillInfo.innerHTML = '两方向EV均负 | 反转=' + reversalOk + ' EV=' + evR + ' | 顺势=' + trendOk + ' EV=' + evT;
    } else if (fillSt && fillSt.indexOf('EV<') === 0) {
      fillInfo.innerHTML = 'EV不足(' + fillSt + ') | 反转EV=' + evR + ' | 顺势EV=' + evT;
    } else if (fillSt === 'Kelly<2.5') {
      fillInfo.innerHTML = 'Kelly<2.5 金额不足 | 反转=' + reversalOk + ' 顺势=' + trendOk;
    } else if (fillSt === 'T<5s') {
      fillInfo.innerHTML = '窗口将关闭 | 反转=' + reversalOk + ' 顺势=' + trendOk;
    } else if (cw.window_id && fillDir) {
      fillInfo.innerHTML = '评估中... 方向=' + fillDir.toUpperCase() + ' | 反转=' + reversalOk + ' 顺势=' + trendOk;
    } else {
      fillInfo.innerHTML = '暂无成交';
    }

    // === Summary data ===
    var sr = await fetch('/api/summary').then(r => r.json());
    var eq = sr.equity != null ? '$' + sr.equity.toFixed(2) : '--';
    setText('cw-eq', eq);

    var rr = await fetch('/api/results').then(r => r.json());
    var items = rr.items || [];

    // Build filled-only list (API returns newest-first, keep that order)
    var filledOnly = [];
    var wins = 0, totalPnl = 0;
    for (var i = 0; i < items.length; i++) {
      if (items[i].won) wins++;
      totalPnl += (items[i].pnl || 0);
      if (items[i].dir) filledOnly.push(items[i]);
    }

    // === Summary bar ===
    var sum = document.getElementById('sum-text');
    var totalWindows = 0;
    if (filledOnly.length > 0) {
      // Oldest = last element (API newest-first, so [last] = oldest)
      var oldestW = parseInt((filledOnly[filledOnly.length - 1].window_id || 'w0').replace('w',''));
      totalWindows = Math.max(1, Math.round((Date.now() - oldestW) / 300000) + 1);
    }
    var covRate = totalWindows > 0 ? (filledOnly.length / totalWindows * 100).toFixed(0) : 0;
    if (items.length) {
      sum.innerHTML = '权益: <strong>' + eq + '</strong> | PnL: <strong class="' + (totalPnl > 0 ? 'pos' : 'neg') + '">' +
        (totalPnl > 0 ? '+' : '') + totalPnl.toFixed(2) + '</strong> | 胜率: <strong>' + wins + '/' + filledOnly.length +
        ' (' + (filledOnly.length > 0 ? (wins/filledOnly.length*100).toFixed(0) : 0) + '%)</strong>' +
        ' | 信号覆盖: <strong>' + filledOnly.length + '/' + totalWindows + ' (' + covRate + '%)</strong>';
    } else {
      sum.innerHTML = '权益: ' + eq + ' | 等待结算...';
    }

    // === Settled table (newest-first, paginated, default page 0 = latest) ===
    window._settledAll = filledOnly;  // newest-first order from API
    window._settledPerPage = 10;
    if (window._settledPage == null) window._settledPage = 0;
    renderSettledPage();

    // === Version ===
    var vr = await fetch('/api/version').then(r => r.json());
    var v = vr.deployed || '?';
    var f = vr.files || {};
    document.getElementById('version').textContent = 'ver: ' + v.slice(0, 16) + ' | lr: ' + (f.live_runner || 0) + 'b';

  } catch(e) {
    document.getElementById('sum-text').textContent = '加载中...';
  }
}

function renderSettledPage() {
  var all = window._settledAll || [];
  var pp = window._settledPerPage || 10;
  var pg = window._settledPage || 0;
  var totalPages = Math.max(1, Math.ceil(all.length / pp));
  var start = pg * pp;
  var pageItems = all.slice(start, start + pp);

  var stb = document.getElementById('settled-body');
  stb.innerHTML = '';
  for (var j = 0; j < pageItems.length; j++) {
    var r = pageItems[j];
    var tr2 = document.createElement('tr');
    var wts3 = 0;
    try { wts3 = parseInt((r.window_id || 'w0').replace('w','')); } catch(e) {}
    var dt2 = new Date(wts3);
    var ttm2 = dt2.getUTCHours().toString().padStart(2,'0')+':'+dt2.getUTCMinutes().toString().padStart(2,'0');
    var seq = r.seq || '';
    var pre3 = seq.length >= 3 ? seq.substring(0,3) : seq;
    var wonMark = r.won ? 'W' : 'L';
    var pnlCls = r.pnl > 0 ? 'pos' : 'neg';
    var amt = r.fill_amt != null ? '$' + r.fill_amt.toFixed(1) : '-';
    var secInWin = r.fill_sec != null ? r.fill_sec + 's' : '-';
    tr2.innerHTML = '<td>' + ttm2 + '</td><td>' + pre3 + '</td><td>' + seq + '</td>' +
      '<td>' + secInWin + '</td><td>' + (r.dir || '') + '</td><td>' + amt + '</td>' +
      '<td><span class="' + pnlCls + '">' + wonMark + ' ' + (r.pnl > 0 ? '+' : '') + r.pnl.toFixed(2) + '</span></td>' +
      '<td>$' + r.equity.toFixed(2) + '</td>';
    stb.appendChild(tr2);
  }
  if (pageItems.length === 0) {
    stb.innerHTML = '<tr><td colspan="8" style="color:#555;text-align:center">暂无已成交结算</td></tr>';
  }

  document.getElementById('settled-page').textContent =
    '(' + (start+1) + '-' + (start+pageItems.length) + ' / ' + all.length + ')';
  document.getElementById('settled-home').style.display = (pg > 0) ? '' : 'none';
  document.getElementById('settled-prev').style.display = (pg > 0) ? '' : 'none';
  document.getElementById('settled-next').style.display = (pg < totalPages - 1) ? '' : 'none';
  document.getElementById('settled-last').style.display = (pg < totalPages - 1) ? '' : 'none';
}

function settledPage(dir) {
  var all = window._settledAll || [];
  var pp = window._settledPerPage || 10;
  var totalPages = Math.max(1, Math.ceil(all.length / pp));
  if (dir === 99) window._settledPage = totalPages - 1;
  else if (dir === -99) window._settledPage = 0;
  else window._settledPage = Math.max(0, Math.min(totalPages - 1, (window._settledPage || 0) + dir));
  renderSettledPage();
}

function setText(id, t) { var el = document.getElementById(id); if (el) el.textContent = t; }
function setClass(id, c) { var el = document.getElementById(id); if (el) el.className = c; }
loadAll();
setInterval(loadAll, 1000);
