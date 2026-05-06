const $=id=>document.getElementById(id);
const money=v=>v==null?'--':'$'+Number(v).toFixed(2);
const num=(v,d=2)=>v==null?'--':Number(v).toFixed(d);
const esc=s=>String(s??'').replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
const ts=ms=>ms?new Date(Number(ms)).toLocaleString('zh-CN',{hour12:false}):'--';
async function api(p){return await (await fetch(p,{cache:'no-store'})).json()}
function pnl(v){return Number(v||0)>=0?'ok':'bad'}
function rows(id,a,f){$(id).innerHTML=a.length?a.map(f).join(''):'<tr><td colspan="9" class="muted">暂无数据</td></tr>'}
async function loadAll(){
  try{
    let [sum,cw,ro,rr,so,sr,lg,ver]=await Promise.all([
      api('/api/summary'),api('/api/current_window'),api('/api/real/orders?n=50'),api('/api/real/results?n=50'),api('/api/shadow/orders?n=50'),api('/api/shadow/results?n=50'),api('/api/logs?n=160'),api('/api/version')
    ]);
    $('health').textContent='正常 '+new Date().toLocaleTimeString('zh-CN',{hour12:false}); $('health').className='pill ok';
    $('m-real').textContent=money(sum.real_balance_usdc); $('m-shadow').textContent=money(sum.shadow_equity_usdc); $('m-window').textContent=cw.window_label||cw.window_id||'--'; $('m-ver').textContent=(ver.deployed||'unknown').slice(0,12);
    $('r-bal').textContent=money(sum.real_balance_usdc); $('r-pend').textContent=money(sum.real_pending_redeem_usdc); $('s-eq').textContent=money(sum.shadow_equity_usdc);
    $('r-fill').textContent=ro.items.filter(x=>x.kind==='order_filled').length; $('r-fail').textContent=ro.items.filter(x=>x.kind==='order_failed').length; $('s-on').textContent=so.items.length; $('s-rn').textContent=sr.items.length;
    let judged=sr.items.filter(x=>x.won===true||x.won===false), wins=judged.filter(x=>x.won).length; $('s-wr').textContent=judged.length?`${wins}/${judged.length} (${(wins/judged.length*100).toFixed(0)}%)`:'--';
    $('current').innerHTML=`<div class="row"><span>窗口</span><b>${esc(cw.window_label||cw.window_id||'--')}</b></div><div class="row"><span>序列</span><b class="mono">${esc(cw.prefix||'--')}</b></div><div class="row"><span>剩余</span><b>${cw.T==null?'--':Math.round(cw.T)+'s'}</b></div><div class="row"><span>方向</span><b>${esc((cw.best_dir||'--').toUpperCase())}</b></div><div class="row"><span>状态</span><b>${esc(cw.status||'等待')}</b></div><div class="row"><span>价格/EV</span><span>up ${num(cw.ask_up,3)} / dn ${num(cw.ask_down,3)} / rev ${num(cw.ev_rev,4)} / trend ${num(cw.ev_trend,4)}</span></div>`;
    rows('real-orders',ro.items,x=>`<tr><td>${ts(x.ts_ms)}</td><td>${esc(x.kind)}</td><td>${esc((x.direction||x.side||'').toUpperCase())}</td><td>${money(x.size_usdc)}</td><td>${num(x.price,4)}</td><td>${esc(x.error||x.last_error||x.status||'')}</td><td class="mono">${esc((x.client_order_id||x.exchange_order_id||'').slice(-26))}</td></tr>`);
    rows('real-results',rr.items,x=>`<tr><td class="mono">${esc(x.window_id)}</td><td>${esc((x.dir||'').toUpperCase())}</td><td>${money(x.fill_amt)}</td><td class="${pnl(x.pnl)}">${num(x.pnl,2)}</td><td>${money(x.equity)}</td></tr>`);
    rows('shadow-orders',so.items,x=>`<tr><td class="mono">${esc(x.window_id)}</td><td class="mono">${esc(x.trigger_pattern||x.seq||'')}</td><td>${esc((x.best_dir||x.dir||'').toUpperCase())}</td><td>${money(x.fill_amount||x.fill_amt)}</td><td>${num(x.best_ev,4)}</td><td>${esc(x.status||'')}</td><td>${esc(x.reason||'')}</td></tr>`);
    rows('shadow-results',sr.items,x=>`<tr><td class="mono">${esc(x.window_id)}</td><td class="mono">${esc(x.seq||'')}</td><td>${esc((x.dir||'').toUpperCase())}</td><td>${money(x.fill_amt)}</td><td class="${pnl(x.pnl)}">${num(x.pnl,2)}</td><td>${money(x.equity)}</td></tr>`);
    $('logbox').textContent=(lg.items||[]).map(x=>x.text||x).join('\n')||'暂无日志';
  }catch(e){$('health').textContent='异常 '+e; $('health').className='pill bad'}
}
document.querySelectorAll('.tab').forEach(b=>b.onclick=()=>{document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));document.querySelectorAll('.sec').forEach(x=>x.classList.remove('active'));b.classList.add('active');$(b.dataset.t).classList.add('active')});
loadAll(); setInterval(loadAll,5000);
