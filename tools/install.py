#!/usr/bin/env python3
"""Unprivileged, non-overwriting installer; subscription credentials stay off argv."""
import argparse
import getpass
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import subprocess
import sys
import urllib.request
import urllib.parse

ROOT = Path(__file__).resolve().parents[1]
HOME = Path.home()
APP = HOME/'.local/share/mihomo-server'
CONFIG = Path(os.environ.get('MIHOMO_SERVER_CONFIG', str(Path(os.environ.get('XDG_CONFIG_HOME',str(HOME/'.config')))/'mihomo-server'))).expanduser().resolve()
UNITS = Path(os.environ.get('XDG_CONFIG_HOME',str(HOME/'.config')))/'systemd/user'
COMMAND = HOME/'.local/bin/mihomo-server'
UNIT_NAMES = ['mihomo-server.service','mihomo-server-update.service','mihomo-server-update.timer']

def arguments():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--mihomo',help='Installed executable (otherwise search PATH)')
    p.add_argument('--nodes',type=Path,help='Import proxies from a local Clash/Mihomo YAML')
    p.add_argument('--subscription-file',type=Path,help='Private file containing the HTTPS subscription URL')
    p.add_argument('--prompt-subscription',action='store_true',help='Read URL with hidden terminal input')
    p.add_argument('--port',type=int,default=17890)
    p.add_argument('--controller-port',type=int,default=19090)
    p.add_argument('--stage-port',type=int,default=27890)
    p.add_argument('--stage-controller-port',type=int,default=29090)
    p.add_argument('--services',default='openai,wandb',help='Comma-separated optional checks: openai,wandb; empty for generic/GitHub only')
    p.add_argument('--activate',action='store_true',help='Network-test and enable user service and timer')
    p.add_argument('--worker',action='store_true',help=argparse.SUPPRESS)
    return p.parse_args()

def preflight(args):
    ports=[args.port,args.controller_port,args.stage_port,args.stage_controller_port]
    if len(set(ports))!=4 or not all(1024<=p<=65535 for p in ports):
        raise RuntimeError('Use four distinct ports between 1024 and 65535')
    if not set(filter(None,args.services.split(','))) <= {'openai','wandb'}:
        raise RuntimeError('Supported optional services: openai,wandb')
    binary=Path(args.mihomo or shutil.which('mihomo') or '').expanduser().resolve()
    if not binary.is_file() or not os.access(binary,os.X_OK):
        raise RuntimeError('Mihomo not found; pass --mihomo /path/to/mihomo')
    for path in [CONFIG,COMMAND,*[UNITS/n for n in UNIT_NAMES]]:
        if path.exists() or path.is_symlink(): raise RuntimeError(f'Refusing to overwrite existing path: {path}')
    if shutil.which('systemctl'):
        for name in UNIT_NAMES:
            existing=subprocess.run(['systemctl','--user','show',name,'-p','FragmentPath','--value'],capture_output=True,text=True,timeout=10)
            if existing.stdout.strip():raise RuntimeError(f'An existing user unit occupies {name}; no services were changed')
    if not args.nodes and not args.subscription_file and not args.prompt_subscription:
        raise RuntimeError('Provide --nodes FILE or a subscription source (--subscription-file FILE / --prompt-subscription)')
    if args.activate:
        r=subprocess.run(['systemctl','--user','show-environment'],capture_output=True,timeout=10)
        if r.returncode: raise RuntimeError('User systemd manager unavailable; install without --activate')
    import socket
    for port in ports:
        try:
            with socket.socket() as sock: sock.bind(('127.0.0.1',port))
        except OSError: raise RuntimeError(f'Port {port} is occupied; existing services were not changed') from None
    return binary

def config_for(args):
    groups=[]
    specs=[('AUTO','https://www.gstatic.com/generate_204','204')]
    selected=set(filter(None,args.services.split(',')))
    if 'openai' in selected: specs.append(('OPENAI','https://api.openai.com/v1/models','401'))
    if 'wandb' in selected: specs.append(('WANDB','https://api.wandb.ai/','200/301/302/404/405'))
    for name,url,status in specs:
        groups.append(dict(name=name,type='fallback',use=['subscription'],url=url,
            **{'expected-status':status,'interval':180,'timeout':8000,'lazy':False,'max-failed-times':2}))
    rules=[]
    if 'openai' in selected: rules += [f'DOMAIN-SUFFIX,{d},OPENAI' for d in ['openai.com','chatgpt.com','oaistatic.com','oaiusercontent.com']]
    if 'wandb' in selected: rules += ['DOMAIN-SUFFIX,wandb.ai,WANDB','DOMAIN,wandb.auth0.com,WANDB']
    rules += ['DOMAIN,localhost,DIRECT']+[f'IP-CIDR,{net},DIRECT,no-resolve' for net in ['127.0.0.0/8','10.0.0.0/8','172.16.0.0/12','192.168.0.0/16']]+['MATCH,AUTO']
    return {'mixed-port':args.port,'allow-lan':False,'bind-address':'127.0.0.1','mode':'rule',
        'external-controller':f'127.0.0.1:{args.controller_port}','secret':secrets.token_urlsafe(32),
        'log-level':'warning','ipv6':False,'tun':{'enable':False},'profile':{'store-selected':False},
        'dns':{'enable':True,'ipv6':False,'enhanced-mode':'redir-host','nameserver':['system'],
               'default-nameserver':['system'],'proxy-server-nameserver':['system']},
        'proxy-providers':{'subscription':{'type':'file','path':'./nodes.yaml','health-check':{
            'enable':True,'url':specs[0][1],'expected-status':'204','interval':180,'timeout':8000,'lazy':False}}},
        'proxy-groups':groups,'rules':rules}

def write(path,text,mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_text(text);path.chmod(mode)

def worker(args,binary):
    import yaml
    sys.path.insert(0,str(ROOT/'src'))
    from mihomo_server import manager as m
    url=''
    if args.subscription_file: url=args.subscription_file.read_text().strip()
    elif args.prompt_subscription: url=getpass.getpass('Subscription HTTPS URL (hidden): ').strip()
    if url and urllib.parse.urlsplit(url).scheme!='https': raise RuntimeError('Subscription source must use HTTPS')
    if args.nodes:
        raw=args.nodes.read_bytes()
    else:
        try:
            opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
            with opener.open(urllib.request.Request(url,headers={'User-Agent':'mihomo'}),timeout=40) as response:
                raw=response.read(8*1024*1024+1)
        except Exception: raise RuntimeError('Initial subscription download failed (URL suppressed)') from None
    try:
        if len(raw)>8*1024*1024: raise ValueError()
        doc=yaml.safe_load(raw);nodes=doc['proxies']
        if not isinstance(nodes,list) or not nodes or len(nodes)>2000: raise ValueError()
        if not all(isinstance(n,dict) and isinstance(n.get('name'),str) and n.get('type') and n.get('server') for n in nodes): raise ValueError()
        if len({n['name'] for n in nodes})!=len(nodes): raise ValueError()
    except Exception: raise RuntimeError('Source must contain a nonempty, unique-name proxies list; provider-only/base64 subscriptions are unsupported') from None
    CONFIG.mkdir(parents=True,mode=0o700)
    write(CONFIG/'.install-owner',os.environ.get('MIHOMO_INSTALL_TOKEN','worker'))
    (CONFIG/'backups').mkdir(mode=0o700);(CONFIG/'state').mkdir(mode=0o700)
    write(CONFIG/'config.yaml',yaml.safe_dump(config_for(args),sort_keys=False))
    write(CONFIG/'nodes.yaml',yaml.safe_dump({'proxies':nodes},allow_unicode=True,sort_keys=False))
    write(CONFIG/'settings.json',json.dumps({'mihomo':str(binary),'subscription_url':url,'download_timeout':40,
        'download_via_proxy':False,'staging_ports':[args.stage_port,args.stage_controller_port],
        'services':list(filter(None,args.services.split(',')))},indent=2))
    m.validate(CONFIG)
    if args.activate:
        print('Checking candidate on isolated ports before activation...',flush=True)
        with m.staging() as (_,c):
            if not all(r['ok'] for r in m.tests(c['mixed-port'])): raise RuntimeError('Initial network tests failed; service was not activated')
    shutil.copytree(ROOT/'src',APP/'src',ignore=shutil.ignore_patterns('__pycache__','*.pyc'))
    runner=f'''#!/bin/sh
export MIHOMO_SERVER_CONFIG={__import__('shlex').quote(str(CONFIG))}
export PYTHONDONTWRITEBYTECODE=1
exec {__import__('shlex').quote(str(APP/'venv/bin/python'))} {__import__('shlex').quote(str(APP/'src/mihomo_server/manager.py'))} "$@"
'''
    write(COMMAND,runner,0o755)
    import shlex
    shell=(ROOT/'shell/proxy.sh.in').read_text().replace('@COMMAND@',shlex.quote(str(COMMAND))).replace('@PORT@',str(args.port))
    write(CONFIG/'shell.sh',shell)
    for name in UNIT_NAMES:
        template=(ROOT/'systemd'/name).read_text()
        # systemd syntax differs from shell syntax: escape specifiers, quotes and backslashes.
        escaped=str(COMMAND).replace('\\','\\\\').replace('"','\\"').replace('%','%%')
        write(UNITS/name,template.replace('@COMMAND@','"'+escaped+'"'),0o644)
    # Manifest records exact owned files; configuration and state survive uninstall.
    files=[COMMAND,*[UNITS/n for n in UNIT_NAMES],*[p for p in APP.rglob('*') if p.is_file() or p.is_symlink()]]
    manifest={'app':str(APP),'command':str(COMMAND),'units':str(UNITS),'files':{
        str(p):hashlib.sha256(b'symlink:'+os.readlink(p).encode() if p.is_symlink() else p.read_bytes()).hexdigest() for p in files},'activated':args.activate}
    write(CONFIG/'install-manifest.json',json.dumps(manifest,indent=2))
    if args.activate:
        subprocess.run(['systemctl','--user','daemon-reload'],check=True)
        enabled_units=[UNIT_NAMES[0]]+([UNIT_NAMES[2]] if url else [])
        subprocess.run(['systemctl','--user','enable','--now',*enabled_units],check=True)
        try:
            import time
            for _ in range(30):
                try:m.api('/version',timeout=1);break
                except Exception:time.sleep(.25)
            else:raise RuntimeError('Production controller unavailable')
            if not all(r['ok'] for r in m.tests()):raise RuntimeError('Production network tests failed')
        except Exception:
            subprocess.run(['systemctl','--user','disable','--now',*UNIT_NAMES[::2]],check=False)
            raise RuntimeError('Activation failed; new service/timer stopped. Private files retained for diagnosis') from None
    print('Installed. Run: source '+shlex.quote(str(CONFIG/'shell.sh')))
    print('Then: proxy on; proxy doctor')
    if not args.activate:print('Service is not enabled. Validate with proxy test after starting, then enable mihomo-server.service and mihomo-server-update.timer.')
    if not url:print('No subscription configured; imported nodes remain usable. Set subscription_url before enabling the update timer.')
    print('Check boot persistence: loginctl show-user "$USER" -p Linger (installer does not change it).')

def main():
    os.umask(0o077)
    args=arguments();binary=preflight(args)
    if args.worker:
        worker(args,binary);return
    if APP.exists() or APP.is_symlink():raise RuntimeError(f'Refusing to overwrite existing application: {APP}')
    APP.mkdir(parents=True,mode=0o700)
    try:
        subprocess.run([sys.executable,'-m','venv',str(APP/'venv')],check=True)
        env={k:v for k,v in os.environ.items() if k.lower() not in {'http_proxy','https_proxy','all_proxy'}}
        subprocess.run([str(APP/'venv/bin/python'),'-m','pip','install','--disable-pip-version-check','-r',str(ROOT/'requirements.txt')],env=env,check=True)
        token=secrets.token_hex(24)
        worker_env=dict(os.environ,MIHOMO_INSTALL_TOKEN=token)
        result=subprocess.run([str(APP/'venv/bin/python'),str(Path(__file__).resolve()),*sys.argv[1:],'--worker'],env=worker_env)
        if result.returncode:raise RuntimeError('Installation did not finish; see the preceding sanitized message')
    except BaseException:
        if not (CONFIG/'install-manifest.json').exists():
            # APP was created by this invocation; never remove pre-existing application/configuration.
            assert APP.resolve()==HOME/'.local/share/mihomo-server' and APP!=HOME
            shutil.rmtree(APP)
            owner=CONFIG/'.install-owner'
            if owner.is_file() and owner.read_text()==locals().get('token'):
                assert CONFIG not in [HOME,Path('/'),ROOT] and not CONFIG.is_symlink()
                shutil.rmtree(CONFIG)
        raise

if __name__=='__main__':
    try:main()
    except KeyboardInterrupt:print('Installation interrupted.',file=sys.stderr);sys.exit(130)
    except Exception as e:
        print(str(e) if isinstance(e,RuntimeError) else 'Installation failed; private inputs and subprocess details suppressed.',file=sys.stderr)
        sys.exit(1)
