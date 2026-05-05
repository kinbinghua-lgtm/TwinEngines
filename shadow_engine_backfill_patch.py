#!/usr/bin/env python3
"""
为shadow_signal_engine添加结算结果回填功能的补丁
"""

# 需要添加的代码片段

BACKFILL_FUNCTION = '''
    def _backfill_window_result(self, window_id: str, baseline_price: float, trigger_direction: str) -> None:
        """
        窗口结束后，回填最终结果
        
        查询Binance K线，判断是否反转
        更新jsonl文件中对应窗口的所有记录
        """
        try:
            import requests
            
            # 解析window_id
            window_start_ms = int(window_id[1:])
            window_end_ms = window_start_ms + 5 * 60 * 1000
            
            # 获取K线数据
            url = 'https://api.binance.com/api/v3/klines'
            params = {
                'symbol': 'BTCUSDT',
                'interval': '1m',
                'startTime': window_start_ms,
                'endTime': window_end_ms + 60000,
                'limit': 10
            }
            
            response = requests.get(url, params=params, timeout=10)
            if response.status_code != 200:
                logger.warning(f"Failed to get klines for {window_id}: {response.status_code}")
                return
            
            klines = response.json()
            if not klines or len(klines) == 0:
                logger.warning(f"No klines for {window_id}")
                return
            
            # 获取窗口结束时的价格
            final_price = float(klines[-1][4])  # close price
            
            # 判断是否反转
            if trigger_direction == 'down':
                reversal = final_price > baseline_price
            else:  # 'up'
                reversal = final_price < baseline_price
            
            logger.info(f"Window {window_id} result: baseline={baseline_price:.2f} final={final_price:.2f} reversal={reversal}")
            
            # 更新jsonl文件
            self._update_jsonl_with_result(window_id, reversal)
            
        except Exception as e:
            logger.warning(f"Failed to backfill result for {window_id}: {e}")
    
    def _update_jsonl_with_result(self, window_id: str, reversal: bool) -> None:
        """
        更新jsonl文件，为指定窗口的所有记录添加final_reversal字段
        """
        try:
            path = self.cfg.jsonl_path
            if not os.path.exists(path):
                return
            
            # 读取所有记录
            records = []
            with open(path, 'r', encoding='utf-8') as f:
                for line in f:
                    try:
                        record = json.loads(line.strip())
                        # 为匹配的窗口添加结果
                        if record.get('window_id') == window_id:
                            record['final_reversal'] = reversal
                        records.append(record)
                    except:
                        pass
            
            # 重写文件
            with open(path, 'w', encoding='utf-8') as f:
                for record in records:
                    f.write(json.dumps(record, ensure_ascii=False, default=str) + '\\n')
            
            logger.info(f"Updated {window_id} with final_reversal={reversal}")
            
        except Exception as e:
            logger.warning(f"Failed to update jsonl for {window_id}: {e}")
'''

ROTATE_WINDOW_PATCH = '''
    def _rotate_window(self, wid_start_ms: int, bar: KlineBar) -> None:
        prev = self._state
        if prev is not None:
            if prev.triggered and prev.signal_count == 0:
                self._emit_audit("shadow_window_no_signal", {
                    "window_id": prev.window_id,
                    "reason": "window_expired_without_signal",
                    "detail": prev.last_no_signal_reason,
                })
            
            # 新增：回填上一个窗口的结果
            if prev.triggered and prev.baseline_price is not None and prev.trigger_direction is not None:
                self._backfill_window_result(
                    prev.window_id,
                    prev.baseline_price,
                    prev.trigger_direction
                )
            
            cb = self.on_window_close
            if cb is not None:
                try:
                    cb(prev.window_id)
                except Exception as e:
                    logger.warning("on_window_close failed wid=%s err=%s", prev.window_id, e)

        self._state = _WindowState(
            window_id=f"w{wid_start_ms}",
            start_ts_ms=int(wid_start_ms),
            end_ts_ms=int(wid_start_ms + WINDOW_SEC * 1000),
        )
        self._windows_seen += 1
        logger.debug("ShadowSignalEngine new window %s", self._state.window_id)
'''

print("="*60)
print("Shadow Signal Engine 结算回填补丁")
print("="*60)
print()
print("需要添加的功能:")
print("1. _backfill_window_result() - 查询Binance并判断结果")
print("2. _update_jsonl_with_result() - 更新jsonl文件")
print("3. 修改_rotate_window() - 在窗口轮换时调用回填")
print()
print("补丁代码已准备好，需要手动应用到:")
print("  src/twinengines/io/shadow_signal_engine.py")
print()
print("或者运行自动补丁脚本...")
