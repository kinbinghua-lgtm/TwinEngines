import paramiko, os, time, json, urllib.request

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('47.243.169.223', username='root', password='Jinbh1977', timeout=10)
sftp = ssh.open_sftp()

files = {
    'scripts/webui_server.py': 'src/twinengines/io/webui_server.py',
    'src/twinengines/io/webui_static/index.html': 'src/twinengines/io/webui_static/index.html',
    'src/twinengines/io/webui_static/app.js': 'src/twinengines/io/webui_static/app.js',
}
for local, remote in files.items():
    lp = os.path.join('E:/TwinEngines', local)
    rp = os.path.join('/root/TwinEngines', remote.replace('/', os.sep))
    sftp.put(lp, rp)
sftp.close()

ssh.exec_command('pkill -9 -f webui_server.py 2>/dev/null')
time.sleep(3)
ssh.exec_command('cd /root/TwinEngines && nohup /root/TwinEngines/.venv/bin/python3 src/twinengines/io/webui_server.py /root/TwinEngines > logs/webui.log 2>&1 &')
time.sleep(5)

r = urllib.request.urlopen('http://47.243.169.223:8080/', timeout=5)
html = r.read().decode()
print(f'Index {r.status}, summary-bar: {"summary-bar" in html}')
r2 = urllib.request.urlopen('http://47.243.169.223:8080/api/results', timeout=5)
d = json.loads(r2.read())
print(f'Results: {len(d["items"])} items')
print('Done')
ssh.close()
