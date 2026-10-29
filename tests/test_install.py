import json
import os
from pathlib import Path
import shlex
import socket
import subprocess
import sys
import tempfile
import unittest
ROOT=Path(__file__).resolve().parents[1]

class Installation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.home=Path(self.temp.name)
        self.bin=self.home/'bin';self.bin.mkdir()
        self.mihomo=self.bin/'mihomo';self.mihomo.write_text('#!/bin/sh\nexit 0\n');self.mihomo.chmod(0o755)
        systemctl=self.bin/'systemctl';systemctl.write_text('#!/bin/sh\nexit 1\n');systemctl.chmod(0o755)
        self.nodes=self.home/'import.yaml';self.nodes.write_text('proxies:\n- name: test-node\n  type: socks5\n  server: node.invalid\n  port: 443\n')
        self.env=dict(os.environ,HOME=str(self.home),XDG_CONFIG_HOME=str(self.home/'.config'),PATH=str(self.bin)+':'+os.environ['PATH'])
        self.env.pop('MIHOMO_SERVER_CONFIG',None)
        socks=[socket.socket() for _ in range(4)]
        try:
            for s in socks:s.bind(('127.0.0.1',0))
            ports=[s.getsockname()[1] for s in socks]
        finally:
            for s in socks:s.close()
        self.args=[sys.executable,str(ROOT/'tools/install.py'),'--worker','--mihomo',str(self.mihomo),'--nodes',str(self.nodes)]
        for flag,port in zip(['--port','--controller-port','--stage-port','--stage-controller-port'],ports):self.args.extend([flag,str(port)])
    def tearDown(self):self.temp.cleanup()
    def run_install(self):return subprocess.run(self.args,env=self.env,capture_output=True,text=True)
    def test_offline_install_and_uninstall_keep_private_data(self):
        p=self.run_install();self.assertEqual(0,p.returncode,p.stderr)
        config=self.home/'.config/mihomo-server';self.assertEqual(0o600,(config/'nodes.yaml').stat().st_mode&0o777)
        self.assertTrue((self.home/'.local/bin/mihomo-server').exists())
        self.assertIn('Restart=on-failure',(self.home/'.config/systemd/user/mihomo-server.service').read_text())
        p=subprocess.run([sys.executable,str(ROOT/'tools/uninstall.py')],env=self.env,capture_output=True,text=True)
        self.assertEqual(0,p.returncode,p.stderr)
        self.assertTrue((config/'nodes.yaml').exists());self.assertFalse((self.home/'.local/bin/mihomo-server').exists())
    def test_existing_config_is_not_overwritten(self):
        config=self.home/'.config/mihomo-server';config.mkdir(parents=True);(config/'sentinel').write_text('keep')
        p=self.run_install();self.assertNotEqual(0,p.returncode);self.assertEqual('keep',(config/'sentinel').read_text())
    def test_uninstall_refuses_modified_unit(self):
        self.assertEqual(0,self.run_install().returncode)
        unit=self.home/'.config/systemd/user/mihomo-server.service';unit.write_text('user modification')
        p=subprocess.run([sys.executable,str(ROOT/'tools/uninstall.py')],env=self.env,capture_output=True,text=True)
        self.assertNotEqual(0,p.returncode);self.assertEqual('user modification',unit.read_text())
    def test_shell_on_off_does_not_need_child_environment_mutation(self):
        source=(ROOT/'shell/proxy.sh.in').read_text().replace('@COMMAND@',shlex.quote(str(self.home/'command with spaces'))).replace('@PORT@','17891')
        path=self.home/'shell.sh';path.write_text(source)
        p=subprocess.run(['bash','-c','unset http_proxy https_proxy; source "$1"; test -z "${http_proxy+x}"; proxy on; test "$https_proxy" = http://127.0.0.1:17891; proxy off; test -z "${https_proxy+x}"','bash',str(path)],env=self.env,capture_output=True,text=True)
        self.assertEqual(0,p.returncode,p.stderr)
