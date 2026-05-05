import pandas as pd, numpy as np
from pathlib import Path

cache = Path('E:/TwinEngines/data_cache/BTCUSDT/1m')
files = sorted(cache.glob('*.parquet'))
df_all = pd.concat([pd.read_parquet(f) for f in files], ignore_index=True)
df_all = df_all.sort_values('open_time').reset_index(drop=True)
W = 5; n_full = (len(df_all) // W) * W; df_all = df_all.iloc[:n_full]

windows = []
for i in range(0, n_full, W):
    block = df_all.iloc[i:i+W]
    bl = float(block.iloc[0]['open'])  # baseline = 1st minute OPEN
    if bl <= 0: continue
    c = [float(x) for x in block['close']]
    s5 = ''.join('1' if c[k] > bl else '0' for k in range(5))
    windows.append({'bl': bl, 'c5': c[4], 's3': s5[:3], 's4': s5[:4], 's5': s5[4]})

print('=' * 65)
print('核心问题：最终方向跟谁走？')
print('=' * 65)

# BY 3RD BIT ALONE
for b3 in ('1', '0'):
    w = [x for x in windows if x['s3'][2] == b3]
    up = sum(1 for x in w if x['c5'] > x['bl'])
    dn = len(w) - up
    print(f'\n  仅知第3位={b3}: n={len(w):5d}  P(up)={up/len(w)*100:5.1f}%  P(down)={dn/len(w)*100:5.1f}%')

# BY MAJORITY ALONE (≥2/3 bits same)
print()
for maj in ('1', '0'):
    w = [x for x in windows if x['s3'].count(maj) >= 2]
    dir_match = sum(1 for x in w if (x['c5'] > x['bl']) == (maj == '1'))
    dir_name = 'up' if maj == '1' else 'down'
    print(f'  仅知多数={maj}(≥2/3): n={len(w):5d}  P({dir_name})={dir_match/len(w)*100:5.1f}%')

# BY 3RD BIT, BROKEN DOWN BY CONTEXT
print('\n' + '=' * 65)
print('按第3位值分组，拆前2位是否同向')
print('=' * 65)

for b3 in ('1', '0'):
    for s3 in sorted(set(x['s3'] for x in windows)):
        if s3[2] != b3: continue
        w = [x for x in windows if x['s3'] == s3]
        up = sum(1 for x in w if x['c5'] > x['bl'])
        dn = len(w) - up
        correct = up if b3 == '1' else dn
        p = correct / len(w) * 100

        # agreement structure
        b12_agree = (s3[0] == b3 and s3[1] == b3)
        if b12_agree:
            ctx = '前2位同向(confirm)'
        elif s3[:2] in ('11', '00'):
            ctx = '前2位同向(oppose) ←断裂!'
        else:
            ctx = '前2位分歧(mixed)'

        star = ' ★★★' if p > 80 else (' ★★' if p > 70 else '')
        print(f'  {s3}  {ctx:25s} n={len(w):5d}  P(第3位对)={p:5.1f}%{star}')

# AHA: "第3位断裂" = 前2位同向 + 第3位逆转
print('\n' + '=' * 65)
print('★ 第3分钟末 "第3位断裂" 信号 (剩余120s)')
print('=' * 65)

for s3 in ['110', '001']:
    w = [x for x in windows if x['s3'] == s3]
    n = len(w)
    rev_dir = 'DOWN' if s3[2] == '0' else 'UP'
    if rev_dir == 'UP':
        p = sum(1 for x in w if x['c5'] > x['bl']) / n * 100
    else:
        p = sum(1 for x in w if x['c5'] <= x['bl']) / n * 100
    per_day = n / 365
    print(f'  {s3}: 前2位={s3[:2]}, 第3位断裂={s3[2]}')
    print(f'    反转方向={rev_dir}  P={p:.1f}%  每天{per_day:.1f}次')

# 第4分钟末 "第4位逆转第3位"
print('\n' + '=' * 65)
print('★ 第4分钟末 "第4位逆转第3位" 信号 (剩余60s)')
print('=' * 65)

rev_s4_signals = []
for s4 in sorted(set(x['s4'] for x in windows)):
    w = [x for x in windows if x['s4'] == s4]
    n = len(w)
    if n < 200: continue
    b3, b4 = s4[2], s4[3]
    if b3 == b4: continue  # no reversal at bit 4

    rev_dir = 'UP' if b4 == '1' else 'DOWN'
    if rev_dir == 'UP':
        p = sum(1 for x in w if x['c5'] > x['bl']) / n * 100
    else:
        p = sum(1 for x in w if x['c5'] <= x['bl']) / n * 100

    per_day = n / 365
    b12_note = '前2同向' if s4[:2] in ('11','00') else ''
    rev_s4_signals.append((s4, rev_dir, p, n, per_day, b12_note))

for s in sorted(rev_s4_signals, key=lambda x: -x[4]):
    print(f'  {s[0]} → 反转={s[1]} P={s[2]:.1f}% n={s[3]} {s[4]:.1f}/天 {s[5]}')

# COMPARISON
print('\n' + '=' * 65)
print('对比：第3末断裂 vs 第4末逆转')
print('=' * 65)
print(f'{"信号":10s} {"触发时刻":12s} {"剩余时间":10s} {"反转P":8s} {"次/天":8s}')
print('-' * 55)
for name, s3, label in [('110', '110', '第3末断裂'), ('001', '001', '第3末断裂')]:
    w = [x for x in windows if x['s3'] == s3]
    rev_dir = 'DOWN' if s3[2] == '0' else 'UP'
    p = sum(1 for x in w if (x['c5'] <= x['bl'] if rev_dir == 'DOWN' else x['c5'] > x['bl'])) / len(w) * 100
    print(f'{name:10s} {label:12s} {"120s":10s} {p:5.1f}%   {len(w)/365:5.1f}')

for s4 in ['1110','0001','1100','0011']:
    w = [x for x in windows if x['s4'] == s4]
    b3, b4 = s4[2], s4[3]
    rev_dir = 'UP' if b4 == '1' else 'DOWN'
    p = sum(1 for x in w if (x['c5'] <= x['bl'] if rev_dir == 'DOWN' else x['c5'] > x['bl'])) / len(w) * 100
    label = '第4末逆转'
    print(f'{s4:10s} {label:12s} {"60s":10s} {p:5.1f}%   {len(w)/365:5.1f}')
