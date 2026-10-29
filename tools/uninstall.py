#!/usr/bin/env python3
"""Remove only unchanged installation-owned files. Keep private configuration."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config',type=Path,default=Path(os.environ.get('MIHOMO_SERVER_CONFIG',str(Path(os.environ.get('XDG_CONFIG_HOME',str(Path.home()/'.config')))/'mihomo-server'))))
    args=parser.parse_args();base=args.config.expanduser().resolve()
    manifest=json.loads((base/'install-manifest.json').read_text())
    home=Path.home().resolve();app=(home/'.local/share/mihomo-server').resolve()
    command=home/'.local/bin/mihomo-server';units=Path(os.environ.get('XDG_CONFIG_HOME',str(home/'.config')))/'systemd/user'
    names=['mihomo-server.service','mihomo-server-update.service','mihomo-server-update.timer']
    if manifest['app']!=str(app):raise RuntimeError('Manifest application path does not match current home')
    owned=[];modified=[]
    for raw,digest in manifest['files'].items():
        p=Path(raw)
        # Do not follow arbitrary manifest paths, symlink directories, or modified service links.
        if not (p==command or p in [units/n for n in names] or p.is_relative_to(app)):
            raise RuntimeError('Manifest contains a path outside this installation')
        if p.exists() or p.is_symlink():
            if p.is_symlink():
                if not p.is_relative_to(app):raise RuntimeError('Installed command/unit replaced with a symlink; inspect manually')
            elif not p.resolve().is_relative_to(app) and p not in [command,*[units/n for n in names]]:
                raise RuntimeError('Installed file escapes application directory')
            content=b'symlink:'+os.readlink(p).encode() if p.is_symlink() else p.read_bytes()
            if hashlib.sha256(content).hexdigest()!=digest:modified.append(p)
            else:owned.append(p)
    if any(p in [command,*[units/n for n in names]] for p in modified):
        raise RuntimeError('Command or unit was modified; uninstall refused to preserve your changes')
    registered=[]
    for name in names:
        result=subprocess.run(['systemctl','--user','show',name,'-p','FragmentPath','--value'],capture_output=True,text=True)
        fragment=result.stdout.strip()
        if fragment and Path(fragment).resolve()==(units/name).resolve():registered.append(name)
        elif fragment:
            raise RuntimeError('User manager now loads a different unit; refusing to alter it')
    if registered:
        subprocess.run(['systemctl','--user','disable','--now','mihomo-server.service','mihomo-server-update.timer'],check=True)
        subprocess.run(['systemctl','--user','stop','mihomo-server-update.service'],check=True)
    for p in owned:p.unlink()
    if app.exists():
        for p in sorted(app.rglob('*'),key=lambda p:len(p.parts),reverse=True):
            if p.is_dir() and not p.is_symlink():
                try:p.rmdir()
                except OSError:pass
        try:app.rmdir()
        except OSError:pass
    if registered:subprocess.run(['systemctl','--user','daemon-reload'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
    (base/'install-manifest.json').rename(base/'uninstalled-manifest.json')
    print('Uninstalled. Private configuration and backups retained at:',base)
    if modified:print('Modified application files retained:',len(modified))
    print('Remove any shell.sh source line you added; open a new shell to clear the proxy function.')

if __name__=='__main__':
    try:main()
    except Exception as e:
        print(str(e) if isinstance(e,RuntimeError) else 'Uninstall failed; no broad directory cleanup was attempted.',file=sys.stderr);sys.exit(1)
