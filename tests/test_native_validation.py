"""Optional native regression tests; no listeners or external requests."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from mihomo_server import manager as m

@unittest.skipUnless(os.environ.get('MIHOMO_TEST_BINARY'),'Set MIHOMO_TEST_BINARY to run installed-binary validation')
class NativeValidation(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.base=Path(self.temp.name)
        self.p=patch.object(m,'BASE',self.base);self.p.start()
        (self.base/'settings.json').write_text(json.dumps({'mihomo':os.environ['MIHOMO_TEST_BINARY']}))
        config={'mixed-port':17890,'allow-lan':False,'bind-address':'127.0.0.1','mode':'rule',
                'proxy-providers':{'subscription':{'type':'file','path':'./nodes.yaml'}},
                'proxy-groups':[{'name':'AUTO','type':'fallback','use':['subscription'],'url':'https://example.invalid/','interval':180}],
                'rules':['MATCH,AUTO']}
        (self.base/'config.yaml').write_text(m.yaml.safe_dump(config))
    def tearDown(self):self.p.stop();self.temp.cleanup()
    def node(self,protocol):
        (self.base/'nodes.yaml').write_text(m.yaml.safe_dump({'proxies':[{'name':'test-node','type':protocol,'server':'node.invalid','port':443}]}))
    def test_unknown_protocol_in_file_provider_is_rejected(self):
        self.node('unsupported-protocol')
        with self.assertRaisesRegex(RuntimeError,'rejected candidate'):m.validate(self.base)
        self.assertEqual([],list(self.base.glob('.validate-*')))
    def test_valid_provider_passes_and_cleans_validation_file(self):
        self.node('socks5');m.validate(self.base)
        self.assertEqual([],list(self.base.glob('.validate-*')))
