import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import patch
from mihomo_server import manager as m

NODE={'name':'example-node','type':'socks5','server':'node.invalid','port':443}
class Response(io.BytesIO):
    def __enter__(self):return self
    def __exit__(self,*args):self.close()

class Updates(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.base=Path(self.tmp.name)
        for d in ['state','backups']:(self.base/d).mkdir()
        self.patches=[patch.object(m,'BASE',self.base),patch.object(m,'STATE',self.base/'state')]
        for p in self.patches:p.start()
        (self.base/'nodes.yaml').write_text(m.yaml.safe_dump({'proxies':[NODE]}))
        (self.base/'config.yaml').write_text('mixed-port: 17890\nexternal-controller: 127.0.0.1:19090\n')
        (self.base/'settings.json').write_text(json.dumps({'subscription_url':'https://subscription.invalid/private','services':[]}))
        self.original=(self.base/'nodes.yaml').read_bytes()
    def tearDown(self):
        for p in reversed(self.patches):p.stop()
        self.tmp.cleanup()
    def unchanged(self):self.assertEqual(self.original,(self.base/'nodes.yaml').read_bytes())
    def response(self,raw):return patch.object(m.DIRECT,'open',return_value=Response(raw))
    def candidate(self):return m.yaml.safe_dump({'proxies':[dict(NODE,port=444)]}).encode()
    def test_failed_download_retains_provider(self):
        with patch.object(m.DIRECT,'open',side_effect=OSError('sensitive-url')):
            with self.assertRaisesRegex(RuntimeError,'download/parse failed'):m.refresh()
        self.unchanged()
    def test_malformed_and_duplicate_subscription_retained(self):
        for payload in [b'proxies: [broken',m.yaml.safe_dump({'proxies':[NODE,NODE]}).encode()]:
            with self.response(payload),self.assertRaises(RuntimeError):m.refresh()
            self.unchanged()
    def test_failed_candidate_does_not_commit(self):
        with self.response(self.candidate()),patch.object(m,'staging',side_effect=RuntimeError('invalid candidate')):
            with self.assertRaises(RuntimeError):m.refresh()
        self.unchanged();self.assertEqual([],list((self.base/'backups').iterdir()))
    def test_live_apply_failure_rolls_back(self):
        stage=contextlib.nullcontext((self.base,{'mixed-port':27890}))
        with self.response(self.candidate()),patch.object(m,'staging',return_value=stage),patch.object(m,'tests',return_value=[{'ok':True}]),patch.object(m.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)),patch.object(m,'api',side_effect=[RuntimeError('apply failed'),None]):
            with self.assertRaisesRegex(RuntimeError,'previous provider restored'):m.refresh()
        self.unchanged();self.assertEqual(1,len(list((self.base/'backups').iterdir())))
    def test_success_keeps_backup_and_private_permissions(self):
        stage=contextlib.nullcontext((self.base,{'mixed-port':27890}))
        with self.response(self.candidate()),patch.object(m,'staging',return_value=stage),patch.object(m,'tests',return_value=[{'ok':True}]),patch.object(m.subprocess,'run',return_value=types.SimpleNamespace(returncode=3)):
            m.refresh()
        self.assertNotEqual(self.original,(self.base/'nodes.yaml').read_bytes())
        self.assertEqual(self.original,next((self.base/'backups').iterdir()).read_bytes())
        self.assertEqual(0o600,(self.base/'nodes.yaml').stat().st_mode&0o777)
    def test_http_status_is_separate_from_transport(self):
        for code,exitcode,ok,fragment in [('401',0,True,'authentication'),('403',0,False,'restriction'),('000',35,False,'TLS')]:
            with patch.object(m.subprocess,'run',return_value=types.SimpleNamespace(stdout=code,returncode=exitcode)):
                result=m.probe(('OpenAI','https://api.openai.com/v1/models',{401}),17890)
            self.assertEqual(ok,result['ok']);self.assertIn(fragment,result['detail'])
    def test_staging_refuses_occupied_port(self):
        import socket
        with socket.socket() as s:
            s.bind(('127.0.0.1',0));s.listen()
            with self.assertRaisesRegex(RuntimeError,'occupied'):m.check_port(s.getsockname()[1])
    def test_restart_rejects_bad_config_before_touching_service(self):
        with patch.object(m.sys,'argv',['mihomo-server','restart']),patch.object(m,'validate',side_effect=RuntimeError('bad config')),patch.object(m.subprocess,'call') as service:
            with self.assertRaisesRegex(RuntimeError,'bad config'):m.main()
            service.assert_not_called()
    def test_subscription_requires_https_before_downloading(self):
        options=json.loads((self.base/'settings.json').read_text());options['subscription_url']='http://subscription.invalid/private'
        (self.base/'settings.json').write_text(json.dumps(options))
        with patch.object(m.DIRECT,'open') as download,self.assertRaisesRegex(RuntimeError,'must use HTTPS'):m.refresh()
        download.assert_not_called();self.unchanged()
