"""Run on VPS: /root/TwinEngines/.venv/bin/python3 _vps_webui_test.py"""
import sys, requests
sys.path.insert(0, "/root/TwinEngines")

s = requests.Session()

# 1. Login page
r = s.get("http://localhost:8080/")
print("Login page:", r.status_code, "size:", len(r.text))
assert 'password' in r.text.lower()

# 2. Submit password
r = s.post("http://localhost:8080/login", data={"password": "Jinbh1977"})
print("Login:", r.status_code, "url:", r.url)
assert r.status_code == 200
assert '/login' not in r.url.lower()

# 3. Dashboard
r = s.get("http://localhost:8080/")
print("Dashboard:", r.status_code, "size:", len(r.text))
assert 'TwinEngines' in r.text

# 4. API
r = s.get("http://localhost:8080/api/shadow_orders?n=10")
data = r.json()
print("API:", r.status_code, "ok=", data.get("ok"), "items=", len(data.get("items", [])))
for item in data.get("items", [])[:3]:
    print("  ", item.get("window_id","")[-8:], item.get("best_dir"), "pnl=", item.get("pnl"))
print("\nALL OK")
