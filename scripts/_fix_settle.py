import paramiko, os, time
ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('47.243.169.223', username='root', password='Jinbh1977', timeout=10)
sftp = ssh.open_sftp()

sftp.get('/root/TwinEngines/src/twinengines/io/live_runner.py', 'E:/TwinEngines/scripts/_lr_tmp.py')
with open('E:/TwinEngines/scripts/_lr_tmp.py', 'r', encoding='utf-8') as f:
    code = f.read()

# Replace the old _on_shadow_window_close that handled _WIN_FILLS
old_start = code.find("    def _on_shadow_window_close(self, window_id: str) -> None:")
old_end = code.find("\n    def _on_bar", old_start)

old_method = code[old_start:old_end]

new_method = """    def _on_shadow_window_close(self, window_id: str) -> None:
        """结算窗口: 查币安结果, 更新资金, 写结果文件。"""
        try:
            import json as _json, urllib.request as _ur
            ws = int(window_id.replace("w",""))
            url = f"https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime={ws}&limit=5"
            data = _json.loads(_ur.urlopen(_ur.Request(url,headers={"User-Agent":"TE/1.0"}),timeout=8).read())
            if len(data) >= 5:
                bl = float(data[0][1])
                seq = "".join("1" if float(k[4])>bl else "0" for k in data)
                actual = "up" if seq[4]=="1" else "down"
                # Get the order from file
                import logging as _lg
                p = "/root/TwinEngines/logs/shadow_orders.jsonl"
                fill_amt = 0.0; best_dir = ""
                if os.path.exists(p):
                    with open(p) as f:
                        for line in f.readlines()[-500:]:
                            if window_id in line:
                                try:
                                    o = _json.loads(line.strip())
                                    if o.get("best_dir"):
                                        best_dir = o["best_dir"]
                                        fill_amt = float(o.get("fill_amount") or o.get("kelly_stake") or 0)
                                except: pass
                won = actual == best_dir if best_dir else False
                pnl = fill_amt * (1.0 - 0.50) if won else -fill_amt
                self._sim_equity += pnl
                res = {"window_id":window_id,"seq":seq,"dir":best_dir,"won":won,"pnl":round(pnl,2),"equity":round(self._sim_equity,2)}
                with open("/root/TwinEngines/logs/window_results.jsonl","a") as f:
                    f.write(_json.dumps(res)+"\\n")
                open("/root/TwinEngines/data_runtime/sim_equity.txt","w").write(str(round(self._sim_equity,2))+"\\n")
                _lg.getLogger(__name__).info("[sim] window=%s seq=%s dir=%s %s pnl=%.2f equity=%.2f",
                    window_id[-10:], seq, best_dir, "W" if won else "L", pnl, self._sim_equity)
        except Exception as e:
            pass

    def _on_bar"""

code = code.replace(old_method, new_method)

with open('E:/TwinEngines/scripts/_lr_tmp.py', 'w', encoding='utf-8') as f:
    f.write(code)
sftp.put('E:/TwinEngines/scripts/_lr_tmp.py', '/root/TwinEngines/src/twinengines/io/live_runner.py')
sftp.close()

ssh.exec_command('rm -f /root/TwinEngines/src/twinengines/io/__pycache__/live_runner*')
ssh.exec_command("kill -9 $(ps aux | grep 'live-run' | grep -v grep | awk '{print $2}') 2>/dev/null")
time.sleep(3)
ssh.exec_command('cd /root/TwinEngines && nohup /root/TwinEngines/.venv/bin/python3 -m src.twinengines.cli live-run --record-shadow-signals --artifact artifact_btc_v6.json > logs/live.log 2>&1 &')
import os; os.remove('E:/TwinEngines/scripts/_lr_tmp.py')
print('Restarted with settlement fix')
ssh.close()
