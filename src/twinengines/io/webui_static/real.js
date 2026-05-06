async function loadAll() {
  try {
    // === Account status ===
    try {
      var bal = await fetch('/api/real/balance').then(r => r.json());
      setText('ac-bal', bal.ok ? '$' + (bal.balance_usdc||0).toFixed(2) : '--');
      setText('ac-pend', bal.ok ? '$' + (bal.pending_redeem||0).toFixed(2) : '--');
      setText('ac-eq', bal.ok ? '$' + ((bal.balance_usdc||0)+(bal.pending_redeem||0)).toFixed(2) : '--');
      var redSt = bal.ok && bal.redeem_ok ? '正常' : '检查';
      setText('ac-red', redSt);
      document.getElementById('ac-red').className = bal.ok && bal.redeem_ok ? 'pos' : 'neg';
    } catch(e) {}

    // === Current window (same data source as shadow) ===
    var cw = await fetch('/api/current_window').then(r => r.json());
    if (cw.ok) {
      setText('cw-seq', cw.prefix || '-');
      setText('cw-T', cw.T != null ? Math.round(cw.T) + 's' : '-');
      setText('cw-au', cw.ask_up != null ? cw.ask_up.toFixed(3) : '-');
      setText('cw-ad', cw.ask_down != null ? cw.ask_down.toFixed(3) : '-');
      setText('cw-d', cw.d_abs != null ? cw.d_abs.toFixed(4) : '-');
      setText('cw-pl', cw.p_lower != null ? cw.p_lower.toFixed(3) : '-');
      setText('cw-evr', cw.ev_rev != null ? cw.ev_rev.toFixed(4) : '-');
      setText('cw-evt', cw.ev_trend != null ? cw.ev_trend.toFixed(4) : '-');
      setText('cw-dir', cw.best_dir ? cw.best_dir.toUpperCase() : '-');
      setText('cw-rd', cw.r_d_ok !== undefined ? (cw.r_d_ok ? 'OK' : 'NO') : '-');
      setText('cw-rp', cw.r_p_ok !== undefined ? (cw.r_p_ok ? 'OK' : 'NO') : '-');
      setClass('cw-rd', cw.r_d_ok ? 'pos' : 'neg');
      setClass('cw-rp', cw.r_p_ok ? 'pos' : 'neg');

      // Status
      var st = cw.status || '';
      if (st === 'FILLED' || st === 'filled') {
        setText('cw-status', '已下单 ' + (cw.best_dir||'').toUpperCase());
        setClass('cw-status', 'signal-ok');
      } else if (st && st.indexOf('R:d>') === 0) {
        setText('cw-status', '反转被拒 d=' + (cw.d_abs||0).toFixed(4) + '>' + (cw.d_cliff||0).toFixed(4));
        setClass('cw-status', 'signal-no');
      } else if (st && st.indexOf('R:p<') === 0) {
        setText('cw-status', '反转被拒 p=' + (cw.p_lower||0).toFixed(3) + '<' + (cw.p_min_r||0).toFixed(3));
        setClass('cw-status', 'signal-no');
      } else if (st === 'EV neg') {
        setText('cw-status', 'EV负'); setClass('cw-status', 'signal-no');
      } else if (st && st.indexOf('EV<') === 0) {
        setText('cw-status', st); setClass('cw-status', 'signal-no');
      } else if (st === 'Kelly<2.5') {
        setText('cw-status', '金额不足'); setClass('cw-status', 'signal-no');
      } else if (st === 'T<5s') {
        setText('cw-status', '窗口结束'); setClass('cw-status', 'signal-wait');
      } else {
        setText('cw-status', '等待窗口...'); setClass('cw-status', 'signal-wait');
      }

      // Fill info bar
      var fi = document.getElementById('fill-info');
      var td = cw.td || '';
      var dAbs = cw.d_abs != null ? cw.d_abs.toFixed(4) : '-';
      var dCliff = cw.d_cliff != null ? cw.d_cliff.toFixed(4) : '-';
      var pLow = cw.p_lower != null ? cw.p_lower.toFixed(3) : '-';
      var pMin = cw.p_min_r != null ? cw.p_min_r.toFixed(3) : '-';
      var evR = cw.ev_rev != null ? cw.ev_rev.toFixed(4) : '-';
      var evT = cw.ev_trend != null ? cw.ev_trend.toFixed(4) : '-';
      var trendOk = (evT !== '-' && parseFloat(evT) > 0) ? 'EV可' : 'EV不可';
      var revOk = (cw.r_d_ok && cw.r_p_ok) ? 'd+p可' : (cw.r_d_ok ? 'd可p否' : (cw.r_p_ok ? 'd否p可' : 'd+p否'));

      if (st === 'FILLED' || st === 'filled') {
        var fa = cw.fill_amt != null ? '$' + cw.fill_amt.toFixed(2) : '-';
        var fask = cw.fill_ask != null ? cw.fill_ask.toFixed(4) : '-';
        var fev = cw.fill_ev != null ? cw.fill_ev.toFixed(4) : '-';
        fi.innerHTML = '已成交: <strong class="pos">' + (cw.best_dir||'').toUpperCase()
          + '</strong> | 金额=' + fa + ' | 买入价=' + fask + ' | EV=' + fev
          + ' | 反转=' + revOk + ' 顺势=' + trendOk;
      } else if (st && st.indexOf('R:d>') === 0) {
        fi.innerHTML = '反转被拒(d): d=' + dAbs + ' > d_cliff=' + dCliff + ' | 顺势=' + trendOk;
      } else if (st && st.indexOf('R:p<') === 0) {
        fi.innerHTML = '反转被拒(p): p_lower=' + pLow + ' < p_min=' + pMin + ' | 顺势=' + trendOk;
      } else if (st === 'EV neg') {
        fi.innerHTML = '两方向EV均负 | 反转EV=' + evR + ' | 顺势EV=' + evT;
      } else if (st && st.indexOf('EV<') === 0) {
        fi.innerHTML = 'EV不足 | 反转EV=' + evR + ' | 顺势EV=' + evT;
      } else if (st === 'Kelly<2.5') {
        fi.innerHTML = 'Kelly<2.5 金额不足 | 反转=' + revOk + ' 顺势=' + trendOk;
      } else if (st === 'T<5s') {
        fi.innerHTML = '窗口将关闭 | 反转=' + revOk + ' 顺势=' + trendOk;
      } else if (cw.window_id) {
        fi.innerHTML = '评估中... | 反转=' + revOk + ' 顺势=' + trendOk;
      } else {
        fi.innerHTML = '暂无成交';
      }
    }

    // === Real account data ===
    var sr = await fetch('/api/summary').then(r => r.json());

    // === Settled results ===
    var rr = await fetch('/api/real_results').then(r => r.json());
    var items = rr.items || [];

    var filledOnly = [];
    var wins = 0, totalPnl = 0;
    for (var i = 0; i < items.length; i++) {
      if (items[i].mode !== 'real') continue;  // 只显示真实盘 (无mode=旧影子记录,也跳过)
      if (items[i].won) wins++;
      totalPnl += (items[i].pnl || 0);
      if (items[i].dir) filledOnly.push(items[i]);
    }

    window._settledAll = filledOnly;
    window._settledPerPage = 10;
    if (window._settledPage == null) window._settledPage = 0;
    renderSettledPage();

    // Real PnL summary
    var comp = document.getElementById('compare-text');
    if (filledOnly.length > 0) {
      comp.innerHTML = '真实 PnL: <strong class="' + (totalPnl>0?'pos':'neg') + '">' + (totalPnl>0?'+':'') + totalPnl.toFixed(2) + '</strong> | 胜率: <strong>' + wins + '/' + filledOnly.length + ' (' + (wins/filledOnly.length*100).toFixed(0) + '%)</strong> | 影子参考: $' + (sr.equity||0).toFixed(2);
    } else {
      comp.innerHTML = '暂无真实成交 | 影子参考: <strong>$' + (sr.equity||0).toFixed(2) + '</strong>';
    }

    // Version
    var vr = await fetch('/api/version').then(r => r.json());
    var v = vr.deployed || '?';
    document.getElementById('version').textContent = 'ver: ' + v.slice(0, 16);

  } catch(e) {
    document.getElementById('fill-info').textContent = '加载中...';
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
    var tr = document.createElement('tr');
    var wts = 0;
    try { wts = parseInt((r.window_id || 'w0').replace('w','')); } catch(e) {}
    var dt = new Date(wts);
    var ttm = dt.getUTCHours().toString().padStart(2,'0')+':'+dt.getUTCMinutes().toString().padStart(2,'0');
    var seq = r.seq || '';
    var pre3 = seq.length >= 3 ? seq.substring(0,3) : seq;
    var wonMark = r.won ? 'W' : 'L';
    var pnlCls = r.pnl > 0 ? 'pos' : 'neg';
    var amt = r.fill_amt != null ? '$' + r.fill_amt.toFixed(1) : '-';
    var secInWin = r.fill_sec != null ? r.fill_sec + 's' : '-';
    tr.innerHTML = '<td>' + ttm + '</td><td>' + pre3 + '</td><td>' + seq + '</td>'
      + '<td>' + secInWin + '</td><td>' + (r.dir || '') + '</td><td>' + amt + '</td>'
      + '<td><span class="' + pnlCls + '">' + wonMark + ' ' + (r.pnl > 0 ? '+' : '') + r.pnl.toFixed(2) + '</span></td>'
      + '<td>$' + r.equity.toFixed(2) + '</td>';
    stb.appendChild(tr);
  }
  if (pageItems.length === 0) {
    stb.innerHTML = '<tr><td colspan="8" style="color:#555;text-align:center">暂无已成交结算</td></tr>';
  }
  document.getElementById('settled-page').textContent = '(' + (start+1) + '-' + (start+pageItems.length) + ' / ' + all.length + ')';
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
