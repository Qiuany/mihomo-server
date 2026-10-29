#!/usr/bin/python3
"""Transactional Mihomo provider updater and local diagnostics."""
import concurrent.futures
import contextlib
import fcntl
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.parse
import urllib.request

import yaml

os.umask(0o077)
if hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(line_buffering=True)
HOME = Path.home()
BASE = Path(os.environ.get('MIHOMO_SERVER_CONFIG', str(Path(os.environ.get('XDG_CONFIG_HOME', str(HOME/'.config'))) / 'mihomo-server'))).expanduser().resolve()
STATE = BASE / 'state'
DIRECT = urllib.request.build_opener(urllib.request.ProxyHandler({}))
SERVICE = 'mihomo-server.service'

def settings():
    return json.loads((BASE/'settings.json').read_text())

def binary():
    return Path(settings()['mihomo']).expanduser()

def ports():
    c = load(BASE/'config.yaml')
    return c['mixed-port'], int(c['external-controller'].rsplit(':',1)[1])

def check_port(port):
    try:
        with socket.socket() as sock:
            sock.bind(('127.0.0.1',port))
    except OSError:
        raise RuntimeError(f'Local port {port} is occupied; no existing process was stopped') from None

def load(path):
    return yaml.safe_load(Path(path).read_text())

def atomic(path, data):
    path = Path(path)
    fd, name = tempfile.mkstemp(prefix='.atomic-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(name, path)
        d = os.open(path.parent, os.O_DIRECTORY)
        try: os.fsync(d)
        finally: os.close(d)
    finally:
        if os.path.exists(name): os.unlink(name)

def api(path, method='GET', data=None, port=None, config=None, timeout=15):
    config = config or load(BASE / 'config.yaml')
    port = port or int(config['external-controller'].rsplit(':',1)[1])
    req = urllib.request.Request(f'http://127.0.0.1:{port}' + path,
        data=None if data is None else json.dumps(data).encode(), method=method,
        headers={'Authorization': 'Bearer ' + config['secret'], 'Content-Type': 'application/json'})
    with DIRECT.open(req, timeout=timeout) as r:
        b = r.read()
        return json.loads(b) if b else None

TARGETS = [('HTTPS', 'https://www.gstatic.com/generate_204', {204}),
           ('OpenAI', 'https://api.openai.com/v1/models', {401}),
           ('GitHub', 'https://github.com/', {200}),
           ('W&B', 'https://api.wandb.ai/', {200, 301, 302, 404, 405})]

def probe(target, port):
    name, url, expected = target
    r = subprocess.run(['curl', '--silent', '--output', '/dev/null', '--write-out', '%{http_code}',
        '--proxy', f'http://127.0.0.1:{port}', '--noproxy', '', '--connect-timeout', '8',
        '--max-time', '18', url], capture_output=True, text=True, timeout=21)
    code = int(r.stdout) if r.stdout.isdigit() else 0
    kind = {5:'proxy DNS failure',6:'destination DNS failure',7:'proxy/tunnel connection failure',
            28:'timeout',35:'TLS handshake failure',60:'TLS certificate failure',56:'tunnel/read failure'}.get(r.returncode, 'transport failure')
    if r.returncode == 0:
        kind = 'OK' if code in expected else ('service restriction (transport OK)' if code == 403 else 'unexpected HTTP status (transport OK)')
        if code == 401: kind = 'authentication required (transport OK)'
    return {'service':name,'http':code,'curl_exit':r.returncode,'detail':kind,'ok':r.returncode == 0 and code in expected}

def tests(port=None, quiet=False):
    port = port or ports()[0]
    enabled = settings().get('services', ['openai','wandb'])
    targets = [t for t in TARGETS if t[0] not in {'OpenAI','W&B'} or (t[0]=='OpenAI' and 'openai' in enabled) or (t[0]=='W&B' and 'wandb' in enabled)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda t: probe(t, port), targets))
    if not quiet:
        for r in results: print(f"{r['service']}: HTTP {r['http']} — {r['detail']}")
    return results

def validate(directory):
    # -t alone does not initialize file providers in all Mihomo versions.
    # Also parse their node definitions as ordinary proxies during validation.
    config = load(directory/'config.yaml')
    config['proxies'] = config.get('proxies',[]) + load(directory/'nodes.yaml')['proxies']
    fd, name = tempfile.mkstemp(prefix='.validate-',suffix='.yaml',dir=directory)
    try:
        with os.fdopen(fd,'w') as stream:
            yaml.safe_dump(config,stream,allow_unicode=True,sort_keys=False)
        r = subprocess.run([str(binary()), '-t', '-d', str(directory), '-f', name],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=25)
        if r.returncode: raise RuntimeError('Mihomo rejected candidate configuration (details suppressed to protect credentials)')
    finally:
        os.unlink(name)

@contextlib.contextmanager
def staging(nodes=None, config=None):
    with open(BASE/'staging.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with _staging(nodes, config) as instance:
            yield instance

@contextlib.contextmanager
def _staging(nodes=None, config=None):
    # Temporary directories stay within the private managed tree and are always removed.
    with tempfile.TemporaryDirectory(prefix='.stage-', dir=BASE) as temp:
        d = Path(temp)
        c = config or load(BASE/'config.yaml')
        c = json.loads(json.dumps(c))
        stage_ports = settings().get('staging_ports',[27890,29090])
        for port in stage_ports: check_port(port)
        c['mixed-port'] = stage_ports[0]
        c['external-controller'] = f'127.0.0.1:{stage_ports[1]}'
        (d/'config.yaml').write_text(yaml.safe_dump(c, sort_keys=False))
        (d/'nodes.yaml').write_text(nodes or (BASE/'nodes.yaml').read_text())
        validate(d)
        proc = subprocess.Popen([str(binary()), '-d', str(d), '-f', str(d/'config.yaml')],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            for _ in range(40):
                if proc.poll() is not None: raise RuntimeError('Staging Mihomo exited')
                try:
                    api('/version', port=stage_ports[1], config=c, timeout=1)
                    break
                except Exception: time.sleep(.25)
            else: raise RuntimeError('Staging controller did not become ready')
            # Native checks are parallel and bounded per node; give initialization time.
            api('/providers/proxies/subscription/healthcheck', port=stage_ports[1], config=c, timeout=150)
            yield d, c
        finally:
            proc.terminate()
            try: proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()

def record(ok, message):
    atomic(STATE/'last-update.json', json.dumps({'time':time.strftime('%Y-%m-%dT%H:%M:%SZ',time.gmtime()),'ok':ok,'message':message}))

def refresh():
    with open(BASE/'update.lock','w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        options = settings()
        url = options.get('subscription_url')
        if not url: raise RuntimeError('No subscription configured: edit private settings.json; imported nodes remain operational')
        if urllib.parse.urlsplit(url).scheme != 'https':
            raise RuntimeError('Subscription must use HTTPS')
        opener = DIRECT
        if options.get('download_via_proxy'):
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({'https':f'http://127.0.0.1:{ports()[0]}'}))
        req = urllib.request.Request(url, headers={'User-Agent':'mihomo/1.19.25'})
        try:
            with opener.open(req, timeout=options.get('download_timeout',40)) as response:
                raw = response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024: raise ValueError()
            doc = yaml.safe_load(raw)
            nodes = doc.get('proxies') if isinstance(doc,dict) else None
            if not isinstance(nodes,list) or not nodes or len(nodes)>2000: raise ValueError()
            names = [p['name'] for p in nodes]
            if len(set(names)) != len(names): raise ValueError()
            if not all(isinstance(p,dict) and p.get('server') and p.get('type') in
                {'anytls','hysteria2','hysteria','ss','ssr','vmess','vless','trojan','tuic','socks5','http','wireguard','mieru','snell'} for p in nodes): raise ValueError()
            nodes = [p for p in nodes if not re.search(r'^(套餐|客服|支持AI[:：]|剩余|距离下次|官网|订阅)',p['name'])]
            if not nodes: raise ValueError()
        except Exception:
            raise RuntimeError('Subscription download/parse failed; current provider retained') from None
        # Preserve established ordering; newly published nodes remain eligible.
        old = load(BASE/'nodes.yaml')['proxies']
        order = {p['name']:i for i,p in enumerate(old)}
        nodes.sort(key=lambda p:order.get(p['name'],len(order)))
        text = yaml.safe_dump({'proxies':nodes},allow_unicode=True,sort_keys=False)
        if text == (BASE/'nodes.yaml').read_text():
            record(True,'Subscription unchanged'); print('Subscription unchanged; current provider retained.'); return
        with staging(text) as (_, staged_config):
            result = tests(staged_config['mixed-port'])
            if not all(r['ok'] for r in result):
                raise RuntimeError('Candidate failed HTTPS/OpenAI/GitHub/W&B validation; current provider retained')
        old_text = (BASE/'nodes.yaml').read_text()
        backup = BASE/'backups'/('nodes-'+time.strftime('%Y%m%dT%H%M%S')+'.yaml')
        atomic(backup, old_text)
        atomic(BASE/'nodes.yaml',text)
        running = subprocess.run(['systemctl','--user','is-active','--quiet',SERVICE]).returncode == 0
        try:
            if running:
                api('/providers/proxies/subscription', 'PUT')
                api('/providers/proxies/subscription/healthcheck', timeout=150)
                if not all(r['ok'] for r in tests(quiet=True)): raise RuntimeError('Post-apply test failed')
        except Exception:
            atomic(BASE/'nodes.yaml',old_text)
            if running:
                try: api('/providers/proxies/subscription','PUT')
                except Exception: subprocess.run(['systemctl','--user','restart',SERVICE],check=True)
            raise RuntimeError('Apply failed; previous provider restored') from None
        # Keep eight provider versions plus the immutable migration backup.
        for p in sorted((BASE/'backups').glob('nodes-*.yaml'))[:-8]: p.unlink()
        record(True,f'Updated {len(nodes)} nodes'); print(f'Validated and applied {len(nodes)} nodes.')

def status():
    active = subprocess.run(['systemctl','--user','is-active',SERVICE],capture_output=True,text=True)
    result = active.returncode
    print('Service:',active.stdout.strip())
    try:
        c = api('/configs'); p = api('/proxies')['proxies']
        print('Routing:',c['mode'])
        for group in load(BASE/'config.yaml')['proxy-groups']:
            print(group['name'], '→',p[group['name']]['now'])
        if c['mode'] != 'rule':
            print('WARNING: runtime mode differs from managed rule mode; run proxy restart')
            result = 1
        health = api('/providers/proxies/subscription')['proxies']
        for g in load(BASE/'config.yaml')['proxy-groups']:
            count=sum(bool(n.get('extra',{}).get(g['url'],{}).get('alive',False)) for n in health)
            checked=sum(bool(n.get('extra',{}).get(g['url'],{}).get('history')) for n in health)
            note=' — checks still running' if checked<len(health) else (' — NO HEALTHY NODES' if count==0 else '')
            print(f"{g['name']}: {count} healthy, {checked}/{len(health)} checked" + note)
        nodes = {n['name']:n for n in load(BASE/'nodes.yaml')['proxies']}
        for g in [group['name'] for group in load(BASE/'config.yaml')['proxy-groups']]:
            node = nodes.get(p[g]['now'])
            if node:
                try: socket.getaddrinfo(node['server'],None); print(g+' upstream DNS: OK')
                except OSError: print(g+' upstream DNS: FAILED')
    except Exception:
        print('Controller unavailable')
        result = 1
    if (STATE/'last-update.json').exists():
        r=json.loads((STATE/'last-update.json').read_text());print('Last update:',r['time'],r['message'])
    print('Recent service warnings/failures:')
    journal=subprocess.run(['journalctl','--user','-u',SERVICE,'-n','100','--no-pager','-o','cat'],capture_output=True,text=True)
    failures=[line for line in journal.stdout.splitlines() if re.search(r'warn|error|fail|timeout',line,re.I) and 'Mihomo shutting down' not in line]
    print('\n'.join(failures[-8:]) if failures else 'None in the last 100 journal entries.')
    return result

def serve():
    directory, filename = BASE, 'config.yaml'
    # Mihomo never sees the subscription URL. Scrub transport logs before journald.
    c=load(directory/filename)
    sensitive=[c['secret']]
    def redact(line):
        line=re.sub(r'https?://[^\s\"\x27]+','[URL redacted]',line)
        for value in sensitive:
            if value: line=line.replace(value,'[credential redacted]')
        return line
    def secrets_from_nodes():
        for n in load(BASE/'nodes.yaml')['proxies']:
            for k in ('password','uuid','private-key','token'):
                if n.get(k): sensitive.append(str(n[k]))
    secrets_from_nodes()
    proc=subprocess.Popen([str(binary()),'-d',str(directory),'-f',str(directory/filename)],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True)
    signal.signal(signal.SIGTERM,lambda *_: proc.terminate())
    signal.signal(signal.SIGINT,lambda *_: proc.terminate())
    for line in proc.stdout:
        # Refresh redaction values after provider updates.
        secrets_from_nodes()
        sensitive[:]=list(set(sensitive))
        print(redact(line),end='',flush=True)
    return proc.wait()

def main():
    cmd=sys.argv[1] if len(sys.argv)>1 else 'status'
    if cmd in ('--help','-h','help'):
        print('Usage: mihomo-server {start|stop|restart|status|test|doctor|update|logs|on|off}'); return 0
    if cmd=='serve': return serve()
    if cmd in ('start','stop','restart'):
        if cmd in ('start','restart'): validate(BASE)
        return subprocess.call(['systemctl','--user',cmd,SERVICE])
    if cmd=='doctor':
        health = status()
        results = tests()
        if all(r['ok'] for r in results): print('Diagnosis: configured services are reachable; check application credentials if problems persist.')
        elif any(r['curl_exit'] for r in results): print('Diagnosis: transport errors detected; inspect DNS, TLS and native group health above.')
        else: print('Diagnosis: transport works; investigate endpoint status/service restrictions.')
        return 0 if health == 0 and all(r['ok'] for r in results) else 1
    if cmd=='status': return status()
    if cmd=='test': return 0 if all(r['ok'] for r in tests()) else 1
    if cmd=='logs': return subprocess.call(['journalctl','--user','-u',SERVICE,'-u','mihomo-server-update.service','-n','60','--no-pager'])
    if cmd=='update':
        try:
            STATE.mkdir(parents=True,exist_ok=True)
            refresh()
        except Exception as e:
            message=str(e) if isinstance(e,RuntimeError) else 'Update failed; inspect local configuration and retry'
            record(False,message);print(message);return 1
        return 0
    if cmd in ('on','off'):
        print('Source your installed shell.sh first; then proxy '+cmd);return 1
    print('Usage: proxy {start|stop|restart|status|test|doctor|update|logs|on|off}');return 2

if __name__=='__main__':
    try: sys.exit(main() or 0)
    except KeyboardInterrupt: sys.exit(130)
    except Exception: print('Operation failed; run proxy status and proxy logs for diagnostics.');sys.exit(1)
