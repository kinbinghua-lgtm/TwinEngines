#!/usr/bin/env python3
"""测试Polymarket API端点"""

import requests
import json
from datetime import datetime

def test_clob_api():
    """测试CLOB API是否有历史数据端点"""
    
    base_url = "https://clob.polymarket.com"
    
    # 先获取一个市场来测试
    print("="*60)
    print("测试 Polymarket CLOB API")
    print("="*60)
    print()
    
    # 测试各种可能的端点
    test_endpoints = [
        # 基础端点
        ("/markets", {}),
        ("/sampling-markets", {}),
        ("/sampling-simplified-markets", {}),
        
        # 可能的历史数据端点
        ("/trades", {"limit": 10}),
        ("/candles", {"interval": "1m", "limit": 10}),
        ("/history", {}),
        ("/orderbook/history", {}),
        ("/market/history", {}),
        ("/data/trades", {}),
        ("/data/candles", {}),
        
        # 其他可能的端点
        ("/prices", {}),
        ("/volumes", {}),
        ("/snapshots", {}),
    ]
    
    results = []
    
    for endpoint, params in test_endpoints:
        url = f"{base_url}{endpoint}"
        try:
            print(f"测试: {endpoint}")
            resp = requests.get(url, params=params, timeout=10)
            
            result = {
                "endpoint": endpoint,
                "status": resp.status_code,
                "success": resp.status_code == 200,
            }
            
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    result["data_type"] = type(data).__name__
                    if isinstance(data, list):
                        result["count"] = len(data)
                        if data:
                            result["sample"] = str(data[0])[:200]
                    elif isinstance(data, dict):
                        result["keys"] = list(data.keys())[:10]
                    print(f"  [OK] 成功 (200) - {result.get('data_type')}")
                    if result.get('count'):
                        print(f"    记录数: {result['count']}")
                except:
                    result["data_type"] = "non-json"
                    print(f"  [OK] 成功 (200) - 非JSON响应")
            elif resp.status_code == 404:
                print(f"  [X] 不存在 (404)")
            elif resp.status_code == 400:
                print(f"  [X] 参数错误 (400)")
            else:
                print(f"  [?] 状态码: {resp.status_code}")
            
            results.append(result)
            
        except requests.exceptions.Timeout:
            print(f"  [X] 超时")
            results.append({"endpoint": endpoint, "status": "timeout"})
        except Exception as e:
            print(f"  [X] 错误: {type(e).__name__}")
            results.append({"endpoint": endpoint, "status": "error", "error": str(e)})
        
        print()
    
    # 总结
    print("="*60)
    print("测试总结")
    print("="*60)
    print()
    
    successful = [r for r in results if r.get("success")]
    if successful:
        print("[OK] 可用的端点:")
        for r in successful:
            print(f"  - {r['endpoint']}")
            if r.get('keys'):
                print(f"    字段: {', '.join(r['keys'][:5])}")
    else:
        print("[X] 未找到可用的历史数据端点")
    
    return results

def test_gamma_api():
    """测试Gamma API"""
    
    print("\n" + "="*60)
    print("测试 Gamma Markets API")
    print("="*60)
    print()
    
    base_url = "https://gamma-api.polymarket.com"
    
    endpoints = [
        "/markets",
        "/events",
        "/markets/history",
        "/trades",
    ]
    
    for endpoint in endpoints:
        url = f"{base_url}{endpoint}"
        try:
            print(f"测试: {endpoint}")
            resp = requests.get(url, timeout=10)
            print(f"  状态码: {resp.status_code}")
            if resp.status_code == 200:
                try:
                    data = resp.json()
                    print(f"  [OK] 数据类型: {type(data).__name__}")
                    if isinstance(data, list) and data:
                        print(f"  [OK] 记录数: {len(data)}")
                except:
                    pass
        except Exception as e:
            print(f"  [X] 错误: {type(e).__name__}")
        print()

def test_thegraph():
    """测试The Graph"""
    
    print("\n" + "="*60)
    print("测试 The Graph")
    print("="*60)
    print()
    
    # The Graph的通用查询端点
    url = "https://api.thegraph.com/subgraphs/name/polymarket/polymarket"
    
    query = """
    {
        markets(first: 5) {
            id
            question
        }
    }
    """
    
    try:
        print("查询 Polymarket 子图...")
        resp = requests.post(url, json={"query": query}, timeout=10)
        print(f"状态码: {resp.status_code}")
        if resp.status_code == 200:
            print("[OK] 找到 Polymarket 子图!")
            data = resp.json()
            print(f"响应: {json.dumps(data, indent=2)[:500]}")
        else:
            print("[X] 子图不存在或名称不正确")
    except Exception as e:
        print(f"[X] 错误: {e}")

if __name__ == "__main__":
    # 测试CLOB API
    clob_results = test_clob_api()
    
    # 测试Gamma API
    test_gamma_api()
    
    # 测试The Graph
    test_thegraph()
    
    # 保存结果
    with open("api_test_results.json", "w") as f:
        json.dump(clob_results, f, indent=2)
    
    print("\n结果已保存到: api_test_results.json")
