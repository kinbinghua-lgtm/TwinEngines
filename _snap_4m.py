#!/usr/bin/env python3
"""等待下一个5分钟窗口，在第4分钟收盘时抓快照"""
import json, time, urllib.request, sys, io
from datetime import datetime, timezone
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

def get_1m_klines():
    url = "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&limit=6"
    req = urllib.request.Request(url, headers={"User-Agent": "TE/1.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read())
    return [{'ot': int(k[0]), 'ct': int(k[6]), 'o': float(k[1]), 'c': float(k[4])} for k in data]

def get_active_market():
    # Try multiple approaches
    urls = [
        "https://gamma-api.polymarket.com/markets?active=true&closed=false&limit=50",
        "https://gamma-api.polymarket.com/events?active=true&limit=20&tag=BTC",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "TE/1.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read())
            if isinstance(data, list):
                for m in data:
                    q = str(m.get("question", ""))
                    if "BTC" in q and ("5m" in q.lower() or "5 min" in q.lower()):
                        tokens = m.get("clob_token_ids", "")
                        if isinstance(tokens, str):
                            tokens = tokens.strip('"').split(',')
                        elif isinstance(tokens, list):
                            tokens = [str(t) for t in tokens]
                        return {
                            'condition_id': m['condition_id'],
                            'question': q,
                            'tokens': tokens,
                        }
            elif isinstance(data, dict):
                events = data.get('data', data.get('events', []))
                for ev in events:
                    markets = ev.get('markets', [])
                    for m in markets:
                        q = str(m.get("question", ""))
                        if "BTC" in q and ("5m" in q.lower() or "5 min" in q.lower()):
                            tokens = m.get("clob_token_ids", "")
                            if isinstance(tokens, str):
                                tokens = tokens.strip('"').split(',')
                            elif isinstance(tokens, list):
                                tokens = [str(t) for t in tokens]
                            return {
                                'condition_id': m['condition_id'],
                                'question': q,
                                'tokens': tokens,
                            }
        except:
            continue
    return None

def get_book(token_id):
    url = f"https://clob.polymarket.com/book?token_id={token_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "TE/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            book = json.loads(resp.read())
        bids = book.get("bids", [])
        asks = book.get("asks", [])
        return {
            'best_bid': float(bids[0]['price']) if bids else None,
            'best_ask': float(asks[0]['price']) if asks else None,
            'best_bid_size': float(bids[0]['size']) if bids else None,
            'best_ask_size': float(asks[0]['size']) if asks else None,
            'bid_depth': len(bids),
            'ask_depth': len(asks),
        }
    except Exception as e:
        return {'error': str(e)}

# Historical reversal rates for all 8 break patterns (year stats)
REV_P = {
    '1110': 73.2, '1100': 83.0, '1010': 74.6, '0110': 75.2,
    '0001': 74.8, '0011': 83.0, '1001': 74.8, '0101': 75.2,
}

TREND_P = {
    '1111': 90.4, '1101': 74.2, '1011': 83.4, '0111': 87.3,
    '0000': 90.2, '0010': 75.3, '1000': 86.3, '0100': 83.1,
}

print("=" * 65)
print(f"启动: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
print("等待下一个5分钟窗口的第4分钟收盘...")
print("=" * 65)

# ── 等下一个窗口开始 ──
now_ms = int(time.time() * 1000)
next_win_start = ((now_ms // 300_000) + 1) * 300_000
wait_start = max(0, (next_win_start - now_ms) / 1000)
print(f"下一个窗口 {wait_start:.0f}s 后开始")

# 等到窗口第4分钟末 = 窗口起点 + 240s + 安全边距
target_ms = next_win_start + 240_000 + 5000  # t=245s, 给bar到达的时间
wait_total = (target_ms - now_ms) / 1000
print(f"将在 {wait_total:.0f}s 后抓快照 (窗口第4分钟末 + 5s缓冲)")

# Wait in smaller increments
while (int(time.time() * 1000) - now_ms) / 1000 < wait_total - 10:
    remaining = wait_total - (int(time.time() * 1000) - now_ms) / 1000
    if int(remaining) % 10 == 0:
        print(f"  剩余 {remaining:.0f}s...")
    time.sleep(5)

# Fine wait for last 10s
while int(time.time() * 1000) < target_ms:
    time.sleep(0.5)

print(f"\n抓快照: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")

# ── 抓取数据 ──
bars = get_1m_klines()
window_start = next_win_start
window_bars = [b for b in bars if window_start <= b['ot'] < window_start + 300_000]

if len(window_bars) < 4:
    print(f"ERROR: 只有 {len(window_bars)} 根窗口内 bar")
    sys.exit(1)

baseline = window_bars[0]['o']
seq_bits = []
for i in range(min(5, len(window_bars))):
    seq_bits.append('1' if window_bars[i]['c'] > baseline else '0')
s4 = ''.join(seq_bits[:4])
s3 = s4[:3]
b3, b4 = s4[2], s4[3]
rev = b3 != b4
rev_dir = ('UP' if b4 == '1' else 'DOWN') if rev else '-'
trend_dir = 'UP' if b3 == '1' else 'DOWN'

print(f"\n窗口 w{window_start}")
print(f"baseline={baseline:.2f}")
print(f"bars: {[(b['o'], b['c']) for b in window_bars]}")
print(f"sequence bits: {seq_bits}")
print(f"s3={s3}  s4={s4}")
print(f"b3={b3}(→{trend_dir}) b4={b4}(→{'REV '+rev_dir if rev else '延续'})")

# ── Polymarket ──
market = get_active_market()
if market is None:
    print("\nPolymarket 获取失败 (可能合约刚到时间，等5s重试)")
    time.sleep(5)
    market = get_active_market()

if market:
    print(f"\n合约: {market['question'][:80]}")
    tokens = market.get('tokens', [])
    if len(tokens) >= 2:
        book_up = get_book(tokens[0])
        book_dn = get_book(tokens[1])
    elif len(tokens) == 1:
        book_up = get_book(tokens[0])
        book_dn = {'error': 'no_no_token'}
    else:
        book_up = {'error': 'no_tokens'}
        book_dn = {'error': 'no_tokens'}
else:
    print("\n无法获取合约")
    book_up = {'error': 'no_market'}
    book_dn = {'error': 'no_market'}

print(f"\nUP token:  ask={book_up.get('best_ask')} bid={book_up.get('best_bid')} "
      f"ask_sz={book_up.get('best_ask_size')} bid_sz={book_up.get('best_bid_size')}")
if book_up.get('error'):
    print(f"  error: {book_up['error']}")

print(f"DOWN token: ask={book_dn.get('best_ask')} bid={book_dn.get('best_bid')} "
      f"ask_sz={book_dn.get('best_ask_size')} bid_sz={book_dn.get('best_bid_size')}")
if book_dn.get('error'):
    print(f"  error: {book_dn['error']}")

# ── EV 评估 ──
print("\n" + "=" * 65)
if rev:
    if rev_dir == 'UP':
        ask = book_up.get('best_ask'); token_id = 0
    else:
        ask = book_dn.get('best_ask'); token_id = 1
    p_rev = REV_P.get(s4)
    if p_rev and ask and ask > 0:
        wp = p_rev / 100
        ev = wp * (1.0 - ask) - (1 - wp) * ask
        print(f"BREAK: s4={s4} rev={rev_dir} hist_P={p_rev:.1f}%")
        print(f"ask={ask:.4f}  EV={ev:+.4f}  EV/本金={ev/ask*100:+.1f}%")
        if ev > 0.03:
            print(f">>> 强正向EV! 值得交易 <<<")
        elif ev > 0:
            print(f"> 微弱正EV")
        else:
            print(f"> 负EV, 盘口已定价")
    else:
        print(f"BREAK: s4={s4} but ask unavailable or no history")
else:
    # 非断裂 - 检查顺势是否还有空间
    trend_p = TREND_P.get(s4, 75)
    if trend_dir == 'UP':
        ask = book_up.get('best_ask')
    else:
        ask = book_dn.get('best_ask')
    print(f"NO BREAK: s4={s4} 趋势延续 P(顺势)={trend_p:.1f}%")
    if ask and ask > 0:
        wp = trend_p / 100
        ev = wp * (1.0 - ask) - (1 - wp) * ask
        print(f"ask={ask:.4f} 顺势EV={ev:+.4f}")
        if ev < 0:
            print(f"> 负EV (和预期一致——顺势token已贵)")

# ── 等待窗末验证 ──
print("\n等待窗末...")
win_end_ms = window_start + 300_000
while int(time.time() * 1000) < win_end_ms + 3000:
    time.sleep(1)

bars3 = get_1m_klines()
win_bars3 = [b for b in bars3 if window_start <= b['ot'] < window_start + 300_000]
if len(win_bars3) >= 5:
    b5_close = win_bars3[4]['c']
    s5 = s4 + ('1' if b5_close > baseline else '0')
    print(f"\n{'=' * 65}")
    print(f"窗口结算: sequence={s5}")
    print(f"baseline={baseline:.2f}  第5分收盘={b5_close:.2f}")

    # 判断实际方向
    final_up = b5_close > baseline
    final_dir = 'UP' if final_up else 'DOWN'

    if rev:
        correct = (rev_dir == final_dir)
        print(f"预测反转={rev_dir}  实际={final_dir}  {'✅ 对了!' if correct else '❌ 错了'}")
    else:
        correct = (trend_dir == final_dir)
        print(f"预测顺势={trend_dir}  实际={final_dir}  {'✅' if correct else '❌'}")

    # 计算理论PnL
    if rev:
        ask = book_up.get('best_ask') if rev_dir == 'UP' else book_dn.get('best_ask')
        if ask and ask > 0:
            if correct:
                pnl = 1.0 - ask
                print(f"理论PnL (ask={ask:.4f}): 赢 = +{pnl:.4f}")
            else:
                pnl = -ask
                print(f"理论PnL (ask={ask:.4f}): 输 = {pnl:.4f}")
else:
    print("\n(窗口bar不足，无法结算)")

print(f"\n完成: {datetime.now(timezone.utc).strftime('%H:%M:%S')} UTC")
