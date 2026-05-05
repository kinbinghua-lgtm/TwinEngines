# Polymarket历史盘口数据获取方案

## 🔍 搜索结果

网络搜索未找到公开的Polymarket历史盘口数据集或API。这可能意味着：
1. Polymarket不提供官方的历史盘口数据下载
2. 数据可能需要自己实时采集
3. 可能有一些非公开的数据源

## 💡 可能的数据获取渠道

### 方案1: Polymarket官方API（最可靠）

#### CLOB API
根据你的代码，Polymarket有CLOB API：

```python
# 当前盘口（实时）
GET https://clob.polymarket.com/book?token_id={token_id}

# 可能的历史数据端点（需要验证）
GET https://clob.polymarket.com/trades?token_id={token_id}
GET https://clob.polymarket.com/candles?token_id={token_id}
```

**行动**:
```bash
# 测试是否有历史交易数据
curl "https://clob.polymarket.com/trades?token_id=YOUR_TOKEN_ID&limit=100"

# 测试是否有K线数据
curl "https://clob.polymarket.com/candles?token_id=YOUR_TOKEN_ID&interval=1m"
```

#### Gamma Markets API
```python
# 你的代码中使用的市场解析器
GET https://gamma-api.polymarket.com/markets?...
```

**可能的端点**:
- `/markets/{id}/history` - 市场历史
- `/markets/{id}/trades` - 历史交易
- `/markets/{id}/orderbook` - 历史盘口快照

### 方案2: 链上数据（Polygon）

Polymarket运行在Polygon链上，所有交易都是链上的。

#### 数据源

**A. Polygon区块浏览器**
- PolygonScan: https://polygonscan.com/
- 可以查询CTF合约的历史交易
- 你的代码中有CTF地址: `platform.ctf_spender`

**B. The Graph（推荐）**
```graphql
# Polymarket可能有The Graph子图
query {
  trades(
    first: 1000
    orderBy: timestamp
    orderDirection: desc
    where: {
      market: "0x..."
    }
  ) {
    id
    timestamp
    price
    amount
    buyer
    seller
  }
}
```

**C. Dune Analytics**
- 可能有Polymarket的公开Dashboard
- 可以查询历史交易数据
- 网址: https://dune.com/

### 方案3: 自己采集（最可控）

#### A. 实时WebSocket采集

你的代码中已经有WebSocket支持：

```python
# src/twinengines/io/polymarket_feed.py
class PolymarketFeed:
    def start(self, token_ids: list[str]) -> None:
        # WebSocket订阅实时盘口
```

**改进方案**:
```python
# 创建专门的数据采集服务
class OrderBookRecorder:
    def __init__(self):
        self.feed = PolymarketFeed(...)
        self.db = sqlite3.connect('orderbook_history.db')
    
    def record_snapshot(self, token_id: str, book: dict):
        """每秒记录一次盘口快照"""
        self.db.execute("""
            INSERT INTO orderbook_snapshots 
            (timestamp, token_id, best_bid, best_ask, bid_size, ask_size)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (time.time(), token_id, ...))
    
    def run_forever(self):
        """持续采集"""
        while True:
            for token_id in active_markets:
                book = self.feed.get_book(token_id)
                self.record_snapshot(token_id, book)
            time.sleep(1)  # 每秒采集
```

#### B. REST API轮询

```python
# 每分钟轮询一次
def collect_orderbook_history():
    resolver = MarketResolver(...)
    poly = PolymarketClient(...)
    
    while True:
        active = resolver.get_active()
        if active:
            # 采集YES和NO的盘口
            book_yes = poly.fetch_book(active.token_id_yes)
            book_no = poly.fetch_book(active.token_id_no)
            
            # 保存到数据库
            save_to_db(timestamp, active.condition_id, book_yes, book_no)
        
        time.sleep(60)  # 每分钟
```

### 方案4: 第三方数据提供商

#### A. Kaiko（加密货币市场数据）
- 网址: https://www.kaiko.com/
- 可能有Polymarket数据
- 通常是付费服务

#### B. CryptoCompare
- 网址: https://www.cryptocompare.com/
- 可能有预测市场数据
- 部分免费API

#### C. Messari
- 网址: https://messari.io/
- 专注于加密货币数据
- 可能有Polymarket相关数据

### 方案5: 学术/研究数据集

#### A. Kaggle
- 搜索: "Polymarket dataset"
- 可能有研究者上传的数据集

#### B. GitHub
- 搜索: "polymarket data" "polymarket scraper"
- 可能有开源的数据采集工具

#### C. 学术论文附带数据
- Google Scholar搜索Polymarket相关论文
- 论文可能附带数据集

## 🎯 推荐方案

### 短期（立即可用）

**方案A: 检查Polymarket CLOB API**

创建测试脚本：

```python
#!/usr/bin/env python3
"""测试Polymarket API是否提供历史数据"""

import requests
import json

def test_clob_endpoints():
    base_url = "https://clob.polymarket.com"
    
    # 获取一个活跃市场的token_id
    # 从你的resolver获取
    
    endpoints = [
        "/trades",
        "/candles", 
        "/history",
        "/orderbook/history",
        "/market/history",
    ]
    
    for endpoint in endpoints:
        url = f"{base_url}{endpoint}"
        try:
            resp = requests.get(url, params={"token_id": "YOUR_TOKEN_ID"})
            print(f"{endpoint}: {resp.status_code}")
            if resp.status_code == 200:
                print(f"  Data: {resp.json()[:200]}")
        except Exception as e:
            print(f"{endpoint}: Error - {e}")

if __name__ == "__main__":
    test_clob_endpoints()
```

**方案B: 查询The Graph**

```bash
# 搜索Polymarket子图
curl https://api.thegraph.com/subgraphs/name/polymarket/...
```

### 中期（1-2周）

**自建数据采集系统**

```python
# 创建 scripts/collect_orderbook.py

import time
import sqlite3
from datetime import datetime
from pathlib import Path

from src.twinengines.io import (
    MarketResolver, MarketResolverCfg,
    PolymarketClient, POLYMARKET_PLATFORM,
    load_polymarket_runtime_cfg
)

class OrderBookCollector:
    def __init__(self, db_path: str = "data_cache/orderbook_history.db"):
        self.db_path = db_path
        self.init_db()
        
        runtime = load_polymarket_runtime_cfg()
        self.poly = PolymarketClient(runtime_cfg=runtime, platform=POLYMARKET_PLATFORM)
        self.resolver = MarketResolver(cfg=MarketResolverCfg(...))
    
    def init_db(self):
        conn = sqlite3.connect(self.db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS orderbook_snapshots (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp INTEGER NOT NULL,
                condition_id TEXT NOT NULL,
                token_id TEXT NOT NULL,
                side TEXT NOT NULL,
                best_bid REAL,
                best_ask REAL,
                bid_size REAL,
                ask_size REAL,
                midpoint REAL,
                spread REAL
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON orderbook_snapshots(timestamp)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_condition ON orderbook_snapshots(condition_id)")
        conn.close()
    
    def collect_snapshot(self):
        """采集当前活跃市场的盘口快照"""
        if not self.resolver.refresh_once():
            return
        
        active = self.resolver.get_active()
        if not active:
            return
        
        timestamp = int(time.time() * 1000)
        
        # 采集YES和NO
        for side, token_id in [("YES", active.token_id_yes), ("NO", active.token_id_no)]:
            book = self.poly.fetch_book(token_id)
            
            if book.get("error"):
                continue
            
            best_bid = book.get("best_bid")
            best_ask = book.get("best_ask")
            spread = (best_ask - best_bid) if (best_bid and best_ask) else None
            
            conn = sqlite3.connect(self.db_path)
            conn.execute("""
                INSERT INTO orderbook_snapshots 
                (timestamp, condition_id, token_id, side, best_bid, best_ask, 
                 bid_size, ask_size, midpoint, spread)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                timestamp,
                active.condition_id,
                token_id,
                side,
                best_bid,
                best_ask,
                book.get("best_bid_size"),
                book.get("best_ask_size"),
                book.get("midpoint"),
                spread
            ))
            conn.commit()
            conn.close()
    
    def run_forever(self, interval_sec: int = 60):
        """持续采集"""
        print(f"Starting orderbook collector (interval={interval_sec}s)")
        while True:
            try:
                self.collect_snapshot()
                print(f"{datetime.now()}: Snapshot collected")
            except Exception as e:
                print(f"Error: {e}")
            time.sleep(interval_sec)

if __name__ == "__main__":
    collector = OrderBookCollector()
    collector.run_forever(interval_sec=60)  # 每分钟采集
```

**部署到VPS**:
```bash
# 创建systemd服务
sudo nano /etc/systemd/system/orderbook-collector.service

[Unit]
Description=Polymarket OrderBook Collector
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=/root/TwinEngines
ExecStart=/root/TwinEngines/.venv/bin/python scripts/collect_orderbook.py
Restart=always

[Install]
WantedBy=multi-user.target

# 启动服务
sudo systemctl enable orderbook-collector
sudo systemctl start orderbook-collector
```

### 长期（1个月+）

**数据积累和分析**

1个月后你将有：
- ~40,000 个盘口快照（每分钟1个 × 60 × 24 × 30）
- 覆盖 ~8,640 个窗口（每天288个 × 30天）
- 足够训练初步的模型

## 📊 数据价值对比

| 数据源 | 优势 | 劣势 | 获取难度 |
|--------|------|------|----------|
| **官方API历史数据** | 完整、准确 | 可能不存在 | ⭐ |
| **链上数据** | 完全透明 | 只有成交，无盘口 | ⭐⭐ |
| **自己采集** | 完全可控 | 需要时间积累 | ⭐⭐⭐ |
| **第三方数据商** | 即时可用 | 昂贵 | ⭐⭐⭐⭐ |

## 🎯 建议行动

1. **今天**: 测试CLOB API是否有历史数据端点
2. **本周**: 部署自己的盘口采集服务
3. **1个月后**: 开始用积累的数据训练模型

---

**结论**: 
- 公开的Polymarket历史盘口数据集可能不存在
- 最可行的方案是**自己采集**
- 建议立即部署采集服务，1个月后就有足够数据训练模型
