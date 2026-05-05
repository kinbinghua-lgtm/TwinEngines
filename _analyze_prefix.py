#!/usr/bin/env python3
"""
全量统计5分钟窗口的prefix模式概率
- 全部32种5位序列的分布
- 前3位→最终方向的条件概率
- 第4位"断裂"后的反转概率
"""

import pandas as pd
import numpy as np
from pathlib import Path
from collections import Counter, defaultdict
import json

# ── 加载数据 ──
cache = Path('E:/TwinEngines/data_cache/BTCUSDT/1m')
files = sorted(cache.glob('*.parquet'))
df_all = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
df_all = df_all.sort_values('open_time').reset_index(drop=True)
print(f'Total bars: {len(df_all)}')

# ── 构建窗口 ──
W = 5  # 5分钟窗口
n_full = (len(df_all) // W) * W
df_all = df_all.iloc[:n_full]

windows = []
for i in range(0, n_full, W):
    block = df_all.iloc[i:i+W]
    baseline = float(block.iloc[0]['open'])
    if baseline <= 0 or not np.isfinite(baseline):
        continue
    closes = [float(x) for x in block['close']]
    seq = ''.join('1' if c > baseline else '0' for c in closes)
    # 最终方向: 第5分钟收盘 vs 基准价 → "正"或"反"于前3位方向
    prefix3 = seq[:3]
    reversed_final = False
    if prefix3 == '111' and closes[4] <= baseline:
        reversed_final = True
    elif prefix3 == '000' and closes[4] > baseline:
        reversed_final = True
    elif prefix3 not in ('111', '000'):
        # 非目标触发: 第5分钟 > baseline → UP
        pass
    windows.append({
        'seq': seq,
        'baseline': baseline,
        'closes': closes,
        'prefix3': prefix3,
        'prefix4': seq[:4],
        'reversed_final': reversed_final,
        'is_target_trigger': prefix3 in ('111', '000'),
    })

print(f'Total windows: {len(windows)}')

# ── 1. 全部分布 ──
seq_counts = Counter(w['seq'] for w in windows)
print('\n═══════════════════════════════════════════')
print('全部32种5位序列分布 (按频率排序)')
print('═══════════════════════════════════════════')
for seq, count in seq_counts.most_common():
    pct = count/len(windows)*100
    print(f'  {seq}: {count:6d} ({pct:5.2f}%)')

# ── 2. 前3位 prefix → 最终方向 ──
print('\n═══════════════════════════════════════════')
print('前3位 prefix → 第5分钟收盘方向')
print('═══════════════════════════════════════════')

prefix3_stats = defaultdict(lambda: {'n': 0, 'up': 0, 'down': 0})
for w in windows:
    s = w['prefix3']
    up = w['closes'][4] > w['baseline']
    prefix3_stats[s]['n'] += 1
    if up:
        prefix3_stats[s]['up'] += 1
    else:
        prefix3_stats[s]['down'] += 1

for s in sorted(prefix3_stats.keys()):
    st = prefix3_stats[s]
    n = st['n']
    if n < 10: continue
    p_up = st['up']/n*100
    p_down = st['down']/n*100
    # "顺势"方向: 111→up, 000→down
    if s[1] == '1':  # 偏涨
        trend_side = 'UP'
        trend_pct = p_up
        rev_pct = p_down
    else:
        trend_side = 'DOWN'
        trend_pct = p_down
        rev_pct = p_up
    print(f'  {s}: n={n:6d}  P(顺势={trend_side})={trend_pct:6.2f}%  P(反转)={rev_pct:5.2f}%')

# ── 3. 仅对 111/000 触发窗口，分析第4位 ──
print('\n═══════════════════════════════════════════')
print('111/000 触发后，第4位→最终反转 (第3分钟末视角)')
print('═══════════════════════════════════════════')

target_windows = [w for w in windows if w['is_target_trigger']]
print(f'触发窗口总数: {len(target_windows)}')

for trigger in ('111', '000'):
    tw = [w for w in target_windows if w['prefix3'] == trigger]
    if not tw: continue
    reverse_side = 'DOWN' if trigger == '111' else 'UP'

    # 第3分钟末: 无条件反转率
    rev_total = sum(1 for w in tw if w['reversed_final'])
    print(f'\n  trigger={trigger} (n={len(tw)})')
    print(f'    第3分钟末 无条件反转率: {rev_total}/{len(tw)} = {rev_total/len(tw)*100:.2f}%')

    # 按第4位的值细分
    for bit4 in ('0', '1'):
        seq4 = trigger + bit4
        tw4 = [w for w in tw if w['prefix4'] == seq4]
        if len(tw4) < 5: continue
        rev4 = sum(1 for w in tw4 if w['reversed_final'])

        # 是否断裂
        is_break = (trigger == '111' and bit4 == '0') or (trigger == '000' and bit4 == '1')
        label = f'第4位={bit4}'
        if is_break:
            label += ' ★断裂★'

        print(f'    {label} (n={len(tw4):5d}): 反转={rev4}/{len(tw4)} = {rev4/len(tw4)*100:.2f}%')

# ── 4. 第3分钟末视角的 "断裂后反转分布" ──
print('\n═══════════════════════════════════════════')
print('第3分钟末: 断裂后各形态反转率')
print('═══════════════════════════════════════════')

for trigger in ('111', '000'):
    tw = [w for w in target_windows if w['prefix3'] == trigger]
    if not tw: continue

    # 断裂子集
    break_bit = '0' if trigger == '111' else '1'
    broken = [w for w in tw if w['seq'][3] == break_bit]
    if not broken: continue

    reverse_side = 'DOWN' if trigger == '111' else 'UP'
    # 断裂后 → 最终反转
    b_rev = sum(1 for w in broken if w['reversed_final'])
    # 断裂后 → 未反转(趋势恢复)
    b_trend = len(broken) - b_rev

    print(f'\n  trigger={trigger} → 第4位={break_bit} (断裂) n={len(broken)}')
    print(f'    反转 (断裂持续至窗末): {b_rev} ({b_rev/len(broken)*100:.1f}%)')
    print(f'    趋势恢复 (打了回去):      {b_trend} ({b_trend/len(broken)*100:.1f}%)')

# ── 5. 第4分钟末视角 (sequence已知4位，剩余60秒) ──
print('\n═══════════════════════════════════════════')
print('第4分钟末: 完整4位序列 → 第5分钟方向')
print('═══════════════════════════════════════════')

prefix4_stats = defaultdict(lambda: {'n': 0, 'up': 0, 'down': 0, 'reversed': 0})
for w in target_windows:
    s4 = w['prefix4']
    up = w['closes'][4] > w['baseline']
    prefix4_stats[s4]['n'] += 1
    if up:
        prefix4_stats[s4]['up'] += 1
    else:
        prefix4_stats[s4]['down'] += 1
    if w['reversed_final']:
        prefix4_stats[s4]['reversed'] += 1

for s4 in sorted(prefix4_stats.keys()):
    st = prefix4_stats[s4]
    n = st['n']
    if n < 5: continue
    trigger = s4[:3]
    if trigger not in ('111', '000'): continue
    trend_side = 'UP' if trigger == '111' else 'DOWN'
    rev_side = 'DOWN' if trigger == '111' else 'UP'

    if trend_side == 'UP':
        trend_pct = st['up']/n*100
        rev_pct = st['down']/n*100
    else:
        trend_pct = st['down']/n*100
        rev_pct = st['up']/n*100

    is_break = (trigger == '111' and s4[3] == '0') or (trigger == '000' and s4[3] == '1')
    label = ''
    if is_break:
        label = ' ★断裂★'

    print(f'  {s4}{label}: n={n:5d}  P(顺势={trend_side})={trend_pct:6.2f}%  P(反转)={rev_pct:5.2f}%')

# ── 6. 按月的稳定性 ──
print('\n═══════════════════════════════════════════')
print('关键前缀概率的月度稳定性')
print('═══════════════════════════════════════════')

df_all['month'] = pd.to_datetime(df_all['open_time'], unit='ms').dt.to_period('M')
months = sorted(df_all['month'].unique())

for month in months[-6:]:  # 最近6个月
    month_bars = df_all[df_all['month'] == month]
    n_m = (len(month_bars) // W) * W
    if n_m < W: continue
    month_bars = month_bars.iloc[:n_m]

    month_wins = []
    for i in range(0, n_m, W):
        block = month_bars.iloc[i:i+W]
        baseline = float(block.iloc[0]['open'])
        if baseline <= 0 or not np.isfinite(baseline): continue
        closes = [float(x) for x in block['close']]
        seq = ''.join('1' if c > baseline else '0' for c in closes)
        month_wins.append(seq)

    print(f'\n  {month} ({len(month_wins)} windows):')
    for prefix in ('111', '1111', '000', '0000'):
        prefix_wins = [w for w in month_wins if w.startswith(prefix)]
        if len(prefix_wins) < 5: continue
        if prefix in ('111', '1111'):
            win_rate = sum(1 for w in prefix_wins if w.endswith('1')) / len(prefix_wins)
        else:
            win_rate = sum(1 for w in prefix_wins if w.endswith('0')) / len(prefix_wins)
        print(f'    {prefix}: n={len(prefix_wins):4d} 胜率={win_rate*100:5.1f}%')

# ── 7. 101/010 等非触发前缀的规律 ──
print('\n═══════════════════════════════════════════')
print('非111/000触发前缀的行为 (第3分钟末)')
print('═══════════════════════════════════════════')

nontarget = [w for w in windows if not w['is_target_trigger']]
for prefix3 in ('110', '101', '100', '011', '010', '001'):
    nt = [w for w in nontarget if w['prefix3'] == prefix3]
    if len(nt) < 10: continue
    up = sum(1 for w in nt if w['closes'][4] > w['baseline'])
    down = len(nt) - up
    # 分析前三位的"偏差方向"
    ones = prefix3.count('1')
    zeros = prefix3.count('0')
    bias = '偏涨' if ones > zeros else '偏跌'
    print(f'  {prefix3} ({bias}): n={len(nt):5d}  P(第5分涨)={up/len(nt)*100:5.1f}%  P(第5分跌)={down/len(nt)*100:5.1f}%')

print('\n═══════════════════════════════════════════')
print('分析完成')
print('═══════════════════════════════════════════')
