const $=id=>document.getElementById(id);
const money=v=>v==null?'--':'$'+Number(v).toFixed(2);
const num=(v,d=2)=>v==null?'--':Number(v).toFixed(d);
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const ts=ms=>ms?new Date(Number(ms)).toLocaleString('zh-CN',{hour12:false}):'--';
const pct=v=>v==null?'--':(Number(v)*100).toFixed(1)+'%';
async function api(p,opt){const r=await fetch(p,{cache:'no-store',...(opt||{})});return await r.json()}
function qs(box){return [...document.querySelectorAll(`#${box} [name]`)].map(i=>`${encodeURIComponent(i.name)}=${encodeURIComponent(i.value||'')}`).join('&')}
function clsPnl(v){return Number(v||0)>=0?'ok':'bad'}
function wonText(v){return v===true?'<span class="ok">赢</span>':v===false?'<span class="bad">输</span>':'<span class="unknown">--</span>'}
function rows(id,a,f,col=9){$(id).innerHTML=a&&a.length?a.map(f).join(''):`<tr><td colspan="${col}" class="muted">暂无数据</td></tr>`}
function setText(id,v){const e=$(id);if(e)e.textContent=v}
function stat(prefix,s){setText(`${prefix}-count`,s.count??0);setText(`${prefix}-wr`,pct(s.win_rate));setText(`${prefix}-pnl`,money(s.total_pnl));setText(`${prefix}-avgpnl`,money(s.avg_pnl));setText(`${prefix}-avgamt`,money(s.avg_amount))}
let liveClock={t:null,at:0};
function tickTime(){if(liveClock.t==null){setText('m-time','--');return}const left=Math.max(0,liveClock.t-(Date.now()-liveClock.at)/1000);setText('m-time',Math.round(left)+'s')}
function statusZh(s){return s==='pass'?'通过':s==='fail'?'未通过':s==='warn'?'注意':'待判断'}
function orderBox(o){return o?`<div class="notice"><b>当前窗口真实 FOK 已成交</b><br>方向：<b>${esc((o.direction||o.side||'').toUpperCase())}</b> ｜ 金额：<b>${money(o.size_usdc||o.amount)}</b> ｜ 价格：<b>${num(o.price,4)}</b><br><span class="muted">订单：${esc(String(o.exchange_order_id||o.client_order_id||'').slice(-32))} ｜ 时间：${ts(o.ts_ms)}</span></div>`:''}
function conds(id,a){$(id).innerHTML=(a||[]).map(x=>`<div class="cond"><b>${esc(x.label)}</b><span class="${esc(x.status)}">${statusZh(x.status)}</span><span>${esc(x.text)}</span></div>`).join('')||'<div class="muted">暂无条件数据</div>'}
async function loadLive(){
  const [sum,cw,dec,ver,st]=await Promise.all([api('/api/summary'),api('/api/current_window'),api('/api/current_decision'),api('/api/version'),api('/api/strategy/status')]);
  $('health').textContent='正常 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});$('health').className='pill ok';
  $('st-run').textContent=st.running?'运行中':'已暂停';$('st-run').className=st.running?'ok':'bad';$('st-pid').textContent='PID '+(st.pid||'--');$('strategy-cmd').textContent=st.command||'--';$('btn-start').disabled=!!st.running;$('btn-stop').disabled=!st.running;
  $('m-real').textContent=money(sum.real_balance_usdc);$('m-shadow').textContent=money(sum.shadow_equity_usdc);$('m-window').textContent=dec.window_label||cw.window_label||'--';$('m-wid').textContent=dec.window_id||cw.window_id||'--';$('m-seq').textContent=dec.seq_display||dec.seq||dec.prefix||cw.seq||cw.prefix||'--';liveClock={t:dec.T_remaining,at:Date.now()};tickTime();$('m-ver').textContent=String(ver.deployed||'unknown').slice(0,12);
  $('real-reason').innerHTML=`当前结论：${esc(dec.real?.reason||'--')}`;$('real-dirbox').innerHTML=`<b>本窗口会同时比较顺势和反转两类机会</b><br>触发方向：<b>${esc(dec.trigger_direction_label||'无')}</b>；顺势候选：<b>${esc(dec.trend_direction_label||'未确定')}</b>，EV=${num(dec.ev_trend,4)}；反转候选：<b>${esc(dec.reversal_direction_label||'未确定')}</b>，EV=${num(dec.ev_rev,4)}。<br>当前选中：<b>${esc(dec.evaluated_direction_label||'未确定')}</b>（${esc(dec.decision_mode||'未确定')}），使用价格：<b>${num(dec.evaluated_ask,4)}</b>。<br><span class="muted">盘口参考：UP=${num(dec.ask_up,4)}，DOWN=${num(dec.ask_down,4)}。除顶部时间外，下面任一关键条件未通过，就不会提交真实 FOK。</span>`;$('real-fillbox').innerHTML=orderBox(dec.real?.filled_order);conds('real-conds',dec.real?.conditions||[]);
  $('shadow-status').textContent=dec.shadow?.status||'--';$('shadow-fill').textContent=money(dec.shadow?.fill_amount);$('shadow-ev').textContent=num(dec.shadow?.ev,4);$('shadow-eq').textContent=money(dec.shadow?.equity);conds('shadow-conds',dec.shadow?.conditions||[]);
}
async function controlStrategy(action){const msg=$('strategy-msg');$('btn-start').disabled=true;$('btn-stop').disabled=true;msg.textContent=action==='start'?'正在启动...':'正在暂停...';try{const j=await api('/api/strategy/'+action,{method:'POST'});msg.textContent=j.ok?(action==='start'?'已启动/启动请求完成':'已暂停/暂停请求完成'):'失败：'+(j.error||j.reason||'unknown');await loadLive()}catch(e){msg.textContent='失败：'+e}}
async function loadReal(){
  const [r,o]=await Promise.all([api('/api/real/results?limit=20&'+qs('real-filters')),api('/api/real/orders?limit=20&'+qs('real-filters'))]);const s=r.stats||{};stat('real',s);$('real-bal').textContent=money(s.real_balance_usdc);$('real-coverage').textContent=pct(s.coverage_rate);$('real-coverage-sub').textContent=`${s.coverage_windows??0}/${s.coverage_total_windows??0} 个5分钟窗口`;
  rows('real-results',r.items,x=>`<tr><td><div>${ts(x.ts_ms)}</div><div class="mono muted">${esc(x.window_id)}</div></td><td class="mono">${esc(x.seq||'')}</td><td>${esc((x.direction||x.dir||'').toUpperCase())}</td><td class="right">${money(x.amount||x.fill_amt)}</td><td class="right ${clsPnl(x.pnl)}">${money(x.pnl)}</td><td>${wonText(x.won)}</td><td class="right">${money(x.real_balance_usdc||x.balance_usdc||x.equity)}</td></tr>`,7);
  rows('real-orders',o.items,x=>`<tr><td>${ts(x.ts_ms)}</td><td>${esc(x.kind)}</td><td class="mono">${esc(x.window_id||'')}</td><td>${esc((x.direction||x.side||'').toUpperCase())}</td><td>${money(x.size_usdc||x.amount)}</td><td>${esc(x.error||x.last_error||x.state||x.status||'')}</td><td class="mono">${esc(String(x.client_order_id||x.exchange_order_id||'').slice(-28))}</td></tr>`,7);
}
async function loadShadow(){
  const [r,o]=await Promise.all([api('/api/shadow/results?limit=20&'+qs('shadow-filters')),api('/api/shadow/orders?limit=20&'+qs('shadow-filters'))]);const s=r.stats||{};$('shadow-count').textContent=s.count??0;$('shadow-wr2').textContent=pct(s.win_rate);$('shadow-pnl').textContent=money(s.total_pnl);$('shadow-avgpnl').textContent=money(s.avg_pnl);$('shadow-avgamt').textContent=money(s.avg_amount);$('shadow-bal').textContent=money(s.shadow_equity_usdc);
  rows('shadow-results',r.items,x=>`<tr><td><div>${ts(x.ts_ms)}</div><div class="mono muted">${esc(x.window_id)}</div></td><td class="mono">${esc(x.seq||'')}</td><td>${esc((x.direction||x.dir||'').toUpperCase())}</td><td class="right">${money(x.amount||x.fill_amt)}</td><td class="right ${clsPnl(x.pnl)}">${money(x.pnl)}</td><td>${wonText(x.won)}</td><td class="right sim">${money(x.equity)}</td></tr>`,7);
  rows('shadow-orders',o.items,x=>`<tr><td><div>${ts(x.ts_ms)}</div><div class="mono muted">${esc(x.window_id)}</div></td><td class="mono">${esc(x.seq||x.trigger_pattern||'')}</td><td>${esc((x.direction||x.best_dir||x.dir||'').toUpperCase())}</td><td>${money(x.amount||x.fill_amount||x.fill_amt)}</td><td>${num(x.best_ev,4)}</td><td>${esc(x.status||'')}</td><td>${esc(x.reason||'')}</td></tr>`,7);
}
async function loadLogs(){const q=encodeURIComponent($('log-q')?.value||''),level=encodeURIComponent($('log-level')?.value||'all');const lg=await api(`/api/logs?n=220&q=${q}&level=${level}`);$('logbox').textContent=(lg.items||[]).map(x=>`[${x.source}] ${x.text}`).join('\n')||'暂无日志'}
async function loadAnalytics(){
  const a=await api('/api/analytics');
  const os=a.orders||{}, rs=a.real_stats||{}, ss=a.shadow_stats||{}, dv=a.divergence||{}, lp=a.low_price||{}, eg=a.exit_guard||{};
  setText('ana-fill-rate',pct(os.fill_rate));setText('ana-fill-sub',`${os.filled??0} 成功 / ${os.events??0} 事件，失败 ${os.failed??0}`);
  setText('ana-real-pnl',money(rs.total_pnl));setText('ana-real-sub',`笔数 ${rs.count??0}，胜率 ${pct(rs.win_rate)}`);
  setText('ana-shadow-pnl',money(ss.total_pnl));setText('ana-shadow-sub',`笔数 ${ss.count??0}，胜率 ${pct(ss.win_rate)}`);
  setText('ana-both',dv.both_windows??0);setText('ana-only-shadow',dv.only_shadow_windows??0);setText('ana-low-count',lp.filled_count??0);
  setText('ana-exit-orders',eg.orders??0);setText('ana-exit-sub',`${eg.filled_or_partial??0} filled/partial，事件 ${eg.events??0}`);
  rows('ana-fail-reasons',os.failure_reasons||[],x=>`<tr><td>${esc(x[0])}</td><td class="right">${esc(x[1])}</td></tr>`,2);
  rows('ana-exit-kinds',eg.by_kind||[],x=>`<tr><td>${esc(x[0])}</td><td class="right">${esc(x[1])}</td></tr>`,2);
  rows('ana-divergence',dv.rows||[],x=>`<tr><td><div>${ts(x.ts_ms)}</div><div class="mono muted">${esc(x.window_id)}</div></td><td>${esc((x.real_dir||'').toUpperCase())}</td><td>${esc((x.shadow_dir||'').toUpperCase())}</td><td class="right ${clsPnl(x.real_pnl)}">${money(x.real_pnl)}</td><td class="right ${clsPnl(x.shadow_pnl)}">${money(x.shadow_pnl)}</td><td class="right ${clsPnl(x.delta_pnl)}">${money(x.delta_pnl)}</td><td>R:${wonText(x.real_won)} S:${wonText(x.shadow_won)}</td></tr>`,7);
  rows('ana-low-entries',lp.items||[],x=>`<tr><td><div>${ts(x.ts_ms)}</div><div class="mono muted">${esc(x.window_id||'')}</div></td><td>${esc((x.direction||x.side||'').toUpperCase())}</td><td class="right">${money(x.size_usdc||x.amount)}</td><td class="right">${num(x.price,4)}</td><td class="mono">${esc(String(x.client_order_id||x.exchange_order_id||'').slice(-32))}</td></tr>`,5);
  const sb=a.shadow_buckets||{};
  const bucketRow=x=>`<tr><td>${esc(x.key)}</td><td class="right">${esc(x.count??0)}</td><td class="right">${pct(x.win_rate)}</td><td class="right ${clsPnl(x.pnl)}">${money(x.pnl)}</td><td class="right">${num(x.avg_price,4)}</td><td class="right">${num(x.avg_ev,3)}</td><td class="right">${money(x.avg_amount)}</td></tr>`;
  const bucketRowShort=x=>`<tr><td>${esc(x.key)}</td><td class="right">${esc(x.count??0)}</td><td class="right">${pct(x.win_rate)}</td><td class="right ${clsPnl(x.pnl)}">${money(x.pnl)}</td><td class="right">${num(x.avg_price,4)}</td><td class="right">${num(x.avg_ev,3)}</td></tr>`;
  rows('ana-shadow-price',sb.by_price||[],bucketRow,7);
  rows('ana-shadow-mode-price',sb.by_mode_price||[],bucketRow,7);
  rows('ana-shadow-time',sb.by_time||[],bucketRowShort,6);
  rows('ana-shadow-prefix',sb.by_prefix||[],bucketRowShort,6);
  const relax=a.relax||{}, rd=relax.depth||{};
  setText('ana-relax-shadow',relax.shadow_count??0);
  setText('ana-relax-real-rate',pct(relax.real_fill_rate));setText('ana-relax-real-sub',`${relax.real_filled??0} 成功 / ${relax.real_events??0} 事件，失败 ${relax.real_failed??0}`);
  setText('ana-relax-depth',money(rd.p50_safe_quote));setText('ana-relax-depth-sub',`样本 ${rd.count??0}，均值 ${money(rd.avg_safe_quote)}，min ${money(rd.min_safe_quote)}，max ${money(rd.max_safe_quote)}`);
  const relaxRow=x=>`<tr><td>${esc(x.key)}</td><td class="right">${esc(x.count??0)}</td><td class="right">${esc(x.filled??0)}</td><td class="right">${pct(x.cf_win_rate)}</td><td class="right ${clsPnl(x.cf_pnl)}">${money(x.cf_pnl)}</td><td class="right">${num(x.avg_price,4)}</td><td class="right">${num(x.avg_ev,3)}</td></tr>`;
  rows('ana-relax-tag',relax.by_tag||[],relaxRow,7);
  rows('ana-relax-price',relax.by_price||[],relaxRow,7);
  const rej=a.rejected||{};
  const rejSummary=x=>`<tr><td>${esc(x.key)}</td><td class="right">${esc(x.raw_count??0)}</td><td class="right">${esc(x.window_count??0)}</td><td class="right">${pct(x.cf_win_rate)}</td><td class="right ${clsPnl(x.cf_pnl)}">${money(x.cf_pnl)}</td><td class="right">${num(x.avg_price,4)}</td><td class="right">${num(x.avg_ev,3)}</td><td class="right">${num(x.avg_d_abs_pct,4)}</td></tr>`;
  const rejShort=x=>`<tr><td>${esc(x.key)}</td><td class="right">${esc(x.raw_count??0)}</td><td class="right">${esc(x.window_count??0)}</td><td class="right">${pct(x.cf_win_rate)}</td><td class="right ${clsPnl(x.cf_pnl)}">${money(x.cf_pnl)}</td><td class="right">${num(x.avg_price,4)}</td></tr>`;
  rows('ana-rej-summary',rej.summary||[],rejSummary,8);
  rows('ana-rej-reason-price',rej.by_reason_price||[],rejShort,6);
  rows('ana-rej-reason-prefix',rej.by_reason_prefix||[],rejShort,6);
  rows('ana-rej-candidates',rej.candidates||[],x=>`<tr><td><div>${ts(x.ts_ms)}</div><div class="mono muted">${esc(x.window_id||'')}</div></td><td>${esc(x.reason||'')}</td><td class="mono">${esc(x.prefix||'')}</td><td>${esc(x.mode||'')}</td><td>${esc((x.direction||'').toUpperCase())}/${esc((x.actual_dir||'--').toUpperCase())}</td><td class="right">${num(x.price,4)}</td><td class="right">${num(x.best_ev,3)}</td><td class="right">${num(x.d_abs_pct,4)}</td><td class="right ${clsPnl(x.cf_pnl_2p5)}">${money(x.cf_pnl_2p5)}</td></tr>`,9);
}
async function loadAll(){try{await Promise.all([loadLive(),loadReal(),loadShadow(),loadAnalytics(),loadLogs()])}catch(e){$('health').textContent='异常 '+e;$('health').className='pill bad'}}
function pageFromPath(){const p=location.pathname.replace('/','')||'live';return ['real','shadow','analytics','logs'].includes(p)?p:'live'}
function setPage(p){document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.page===p));document.querySelectorAll('.sec').forEach(s=>s.classList.toggle('active',s.id===p));history.replaceState(null,'',p==='live'?'/':'/'+p)}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>setPage(b.dataset.page));
document.querySelectorAll('#real-filters input,#real-filters select').forEach(el=>{el.addEventListener('change',loadReal);el.addEventListener('input',()=>{clearTimeout(window.__realFilterTimer);window.__realFilterTimer=setTimeout(loadReal,500)})});
document.querySelectorAll('#shadow-filters input,#shadow-filters select').forEach(el=>{el.addEventListener('change',loadShadow);el.addEventListener('input',()=>{clearTimeout(window.__shadowFilterTimer);window.__shadowFilterTimer=setTimeout(loadShadow,500)})});
setPage(pageFromPath());loadAll();setInterval(tickTime,1000);setInterval(loadLive,5000);setInterval(()=>{const p=pageFromPath(); if(p==='real')loadReal(); else if(p==='shadow')loadShadow(); else if(p==='analytics')loadAnalytics(); else if(p==='logs')loadLogs();},5000);
