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
function conds(id,a){$(id).innerHTML=(a||[]).map(x=>`<div class="cond"><b>${esc(x.label)}</b><span class="${esc(x.status)}">${esc(x.status)}</span><span>${esc(x.text)}</span></div>`).join('')||'<div class="muted">暂无条件数据</div>'}
async function loadLive(){
  const [sum,cw,dec,ver,st]=await Promise.all([api('/api/summary'),api('/api/current_window'),api('/api/current_decision'),api('/api/version'),api('/api/strategy/status')]);
  $('health').textContent='正常 '+new Date().toLocaleTimeString('zh-CN',{hour12:false});$('health').className='pill ok';
  $('st-run').textContent=st.running?'运行中':'已暂停';$('st-run').className=st.running?'ok':'bad';$('st-pid').textContent='PID '+(st.pid||'--');$('strategy-cmd').textContent=st.command||'--';$('btn-start').disabled=!!st.running;$('btn-stop').disabled=!st.running;
  $('m-real').textContent=money(sum.real_balance_usdc);$('m-shadow').textContent=money(sum.shadow_equity_usdc);$('m-window').textContent=dec.window_label||cw.window_label||'--';$('m-wid').textContent=dec.window_id||cw.window_id||'--';$('m-time').textContent=dec.T_remaining==null?'--':Math.round(dec.T_remaining)+'s';$('m-ver').textContent=String(ver.deployed||'unknown').slice(0,12);
  $('real-reason').textContent=dec.real?.reason||'--';$('real-dirbox').innerHTML=`触发方向：<b>${esc(dec.trigger_direction_label||'无')}</b> ｜ 本次评估方向：<b>${esc(dec.evaluated_direction_label||'未确定')}</b> ｜ 类型：<b>${esc(dec.decision_mode||'未确定')}</b> ｜ 使用价格：<b>${num(dec.evaluated_ask,4)}</b><br><span class="muted">UP 价格=${num(dec.ask_up,4)}，DOWN 价格=${num(dec.ask_down,4)}。策略会在两个方向里选择当前 EV 更优且条件满足的一边。</span>`;conds('real-conds',dec.real?.conditions||[]);
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
async function loadAll(){try{await Promise.all([loadLive(),loadReal(),loadShadow(),loadLogs()])}catch(e){$('health').textContent='异常 '+e;$('health').className='pill bad'}}
function pageFromPath(){const p=location.pathname.replace('/','')||'live';return ['real','shadow','logs'].includes(p)?p:'live'}
function setPage(p){document.querySelectorAll('.tab').forEach(b=>b.classList.toggle('active',b.dataset.page===p));document.querySelectorAll('.sec').forEach(s=>s.classList.toggle('active',s.id===p));history.replaceState(null,'',p==='live'?'/':'/'+p)}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>setPage(b.dataset.page));
document.querySelectorAll('#real-filters input,#real-filters select').forEach(el=>{el.addEventListener('change',loadReal);el.addEventListener('input',()=>{clearTimeout(window.__realFilterTimer);window.__realFilterTimer=setTimeout(loadReal,500)})});
document.querySelectorAll('#shadow-filters input,#shadow-filters select').forEach(el=>{el.addEventListener('change',loadShadow);el.addEventListener('input',()=>{clearTimeout(window.__shadowFilterTimer);window.__shadowFilterTimer=setTimeout(loadShadow,500)})});
setPage(pageFromPath());loadAll();setInterval(()=>{loadLive(); if(pageFromPath()==='real')loadReal();},5000);
