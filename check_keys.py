from fabric import Connection
import json

conn = Connection('47.243.169.223', user='root', connect_kwargs={'password': 'Jinbh1977'})
result = conn.run('grep prediction /root/TwinEngines/logs/strategy_stdout.log | grep submitted | head -1', hide=True)
data = json.loads(result.stdout.strip())
print('Keys:', list(data.keys()))
if 'quote_snapshot' in data:
    print('Has quote_snapshot')
else:
    print('No quote_snapshot')
    if 'quote' in data:
        print('Has quote')
conn.close()
