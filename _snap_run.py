import sys, json, time, urllib.request
sys.path.insert(0, 'E:/TwinEngines')
from src.twinengines.io.config import load_polymarket_runtime_cfg
from src.twinengines.io.polymarket_client import PolymarketClient, POLYMARKET_PLATFORM
from src.twinengines.io.market_resolver import MarketResolver, MarketResolverCfg

def klines():
    r = urllib.request.Request('https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=6',
                                headers={'User-Agent':'TE/1.0'})
    with urllib.request.urlopen(r, timeout=10) as resp:
        d = json.loads(resp.read())
    return [{'ot':int(k[0]),'ct':int(k[6]),'o':float(k[1]),'c':float(k[4])} for k in d]

# init resolver + client once
resolver = MarketResolver(cfg=MarketResolverCfg(request_timeout_sec=12))
resolver.refresh_once()
rt = load_polymarket_runtime_cfg()
client = PolymarketClient(runtime_cfg=rt, platform=POLYMARKET_PLATFORM)
client.attach_feed(None)

def run_one():
    now = int(time.time()*1000)
    ws = (now // 300000) * 300000
    sec_in = (now - ws) / 1000
    target = ws + 246000
    wait = max(0, (target - now)/1000)
    if wait > 240:
        print('  skip: too long wait')
        return None
    if wait > 3:
        time.sleep(wait)

    kb = klines()
    wb = [b for b in kb if ws <= b['ot'] < ws + 300000]
    if len(wb) < 4: return None
    bl = wb[0]['o']
    s4 = ''.join('1' if wb[i]['c'] > bl else '0' for i in range(4))
    b3, b4 = s4[2], s4[3]
    rev = b3 != b4
    rdir = ('UP' if b4 == '1' else 'DOWN') if rev else '-'
    tdir = 'UP' if b3 == '1' else 'DOWN'

    m = resolver.get_active()
    bu, bd = {}, {}
    if m:
        bu = client.fetch_book(m.token_id_yes)
        bd = client.fetch_book(m.token_id_no)

    # 等窗末
    we = ws + 303000
    w2 = max(0, (we - int(time.time()*1000))/1000)
    if 0 < w2 < 200: time.sleep(w2 + 2)

    kb2 = klines()
    wb2 = [b for b in kb2 if ws <= b['ot'] < ws + 300000]
    s5 = ''
    final_dir = ''
    if len(wb2) >= 5:
        s5 = s4 + ('1' if wb2[4]['c'] > bl else '0')
        final_dir = 'UP' if wb2[4]['c'] > bl else 'DOWN'

    return {
        's4': s4, 'b3': b3, 'b4': b4, 'rev': rev,
        'rdir': rdir, 'tdir': tdir,
        'ask_up': bu.get('best_ask'), 'ask_dn': bd.get('best_ask'),
        'bid_up': bu.get('best_bid'), 'bid_dn': bd.get('best_bid'),
        's5': s5, 'final_dir': final_dir,
        'correct': (rdir == final_dir) if rev else (tdir == final_dir),
    }

results = []
for rnd in range(8):
    print(f'\nround {rnd+1}:')
    r = run_one()
    if r is None:
        print('  skipped')
        continue
    results.append(r)
    rev_label = f'REV {r["rdir"]}' if r['rev'] else 'TREND'
    correct_label = 'OK' if r['correct'] else 'WRONG'
    ask = r['ask_up'] if (r['rev'] and r['rdir']=='UP') else r['ask_dn'] if r['rev'] else None
    print(f'  s4={r["s4"]} {rev_label} ask_up={r["ask_up"]} ask_dn={r["ask_dn"]} -> final={r["final_dir"]} {correct_label}')
    if r['rev'] and ask:
        pr = {'1110':73.2,'1100':83.0,'1010':74.6,'0110':75.2,'0001':74.8,'0011':83.0,'1001':74.8,'0101':75.2}.get(r['s4'])
        if pr:
            ev = pr/100*(1-ask) - (1-pr/100)*ask
            print(f'    histP={pr:.1f}% ask={ask:.4f} EV={ev:+.4f}')

print('\n========== SUMMARY ==========')
for i, r in enumerate(results):
    rev_label = f'REV {r["rdir"]}' if r['rev'] else 'TREND'
    print(f'  {i+1}. s4={r["s4"]} {rev_label} -> {r["final_dir"]} {"OK" if r["correct"] else "WRONG"}')
n_ok = sum(1 for r in results if r['correct'])
print(f'\nTotal: {len(results)} windows, {n_ok}/{len(results)} correct ({n_ok/len(results)*100:.0f}%)')
