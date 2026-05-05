import paramiko, os, time, json, urllib.request

ssh = paramiko.SSHClient()
ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
ssh.connect('47.243.169.223', username='root', password='Jinbh1977', timeout=10)
sftp = ssh.open_sftp()

files = {
    'src/twinengines/io/webui_static/index.html': '/root/TwinEngines/src/twinengines/io/webui_static/index.html',
    'src/twinengines/io/webui_static/app.js': '/root/TwinEngines/src/twinengines/io/webui_static/app.js',
    'src/twinengines/io/_final_api.py': '/root/TwinEngines/src/twinengines/io/_final_api.py',
}
for local, remote in files.items():
    sftp.put(os.path.join('E:/TwinEngines', local), remote)
sftp.close()

ssh.exec_command('kill -9 $(ps aux | grep _final | grep -v grep | awk "{print $2}") 2>/dev/null; sleep 2')
ssh.exec_command('cd /root/TwinEngines && nohup /root/TwinEngines/.venv/bin/python3 src/twinengines/io/_final_api.py /root/TwinEngines > logs/webui.log 2>&1 &')
time.sleep(3)

r = urllib.request.urlopen('http://47.243.169.223:8080/api/summary', timeout=5)
d = json.loads(r.read())
print(f"Summary: equity={d['equity']}")
print("Ready: http://47.243.169.223:8080")
ssh.close()
