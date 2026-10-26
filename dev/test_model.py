import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from model import identity, pending, summarize, pair_tag, recipe, expand

class Gates(unittest.TestCase):
    def evidence(self, failure=None):
        return {'inventory':['a.py::test_a'], 'exitstatus':1 if failure else 0,
                'events':[{'nodeid':'a.py::test_a','phase':p,'outcome':'failed' if failure and p=='call' else 'passed',
                           'detail':failure if failure and p=='call' else '', 'xfail':None} for p in ('setup','call','teardown')]}

    def test_new_base_python39_does_not_schedule_unlocked_overlay(self):
        import itertools
        cells=[]
        for u,c,p,a in itertools.product(('22.04','24.04'),('9.0.0','9.2.0'),('3.9','3.10','3.11','3.12'),('amd64','arm64')):
            cells.append(dict(key=f'{u}-{c}-{p}-{a}', id=str(len(cells)), status='pass', source_sha='source',
                              version='1', digest='sha256:'+str(len(cells)),
                              image='ghcr.io/compiler-team-ru/pyasc-sim@sha256:'+str(len(cells)),
                              environment=dict(ubuntu=u,cann=c,python=p,arch=a)))
        expanded=expand(dict(cells=cells,source_sha='source',recipe='base'), 'overlay')
        self.assertEqual(len(pending(expanded, [])), 24)
        exclusions=[c for c in expanded if 'incompatibility' in c]
        self.assertEqual(len(exclusions), 8)
        self.assertTrue(all(c['base']['status']=='pass' for c in exclusions))
        self.assertTrue(all('dependency lock' in c['incompatibility'] for c in exclusions))

    def test_success(self):
        e=self.evidence()
        self.assertTrue(summarize(e,e['inventory'])['publishable'])

    def test_unknown_failure_blocks(self):
        e=self.evidence('compiler error')
        self.assertFalse(summarize(e,e['inventory'])['publishable'])

    def test_exact_diagnosis(self):
        e=self.evidence('compiler error')
        d={'nodeid':e['inventory'][0],'detail_sha256':hashlib.sha256(b'compiler error').hexdigest(),'category':'compiler','reason':'Reviewed diagnostic'}
        self.assertTrue(summarize(e,e['inventory'],[d])['publishable'])
        d['category']='dependency'
        self.assertFalse(summarize(e,e['inventory'],[d])['publishable'])

    def test_incomplete_blocks(self):
        e=self.evidence();e['events'].pop()
        with self.assertRaises(AssertionError): summarize(e,e['inventory'])

    def test_inventory_change_blocks(self):
        with self.assertRaises(AssertionError): summarize(self.evidence(),['other'])

    def test_skips_visible(self):
        e=self.evidence();e['events'][1].update(outcome='skipped',xfail='upstream reason')
        self.assertEqual(summarize(e,e['inventory'])['counts'],{'xfail':1})

    def test_partial_retry(self):
        cells=[{'id':str(i),'key':str(i),'base':{'status':'pass'}} for i in range(3)]
        self.assertEqual([c['id'] for c in pending(cells,[{'id':'0','status':'published'},{'id':'1','status':'blocked'}])],['1','2'])
        self.assertEqual(pending(cells,[{**c,'status':'published'} for c in cells]),[])

    def test_mixed_pair(self):
        def cell(arch):
            return {'status':'published','recipe':'dev','tag':'image:version-'+arch,'base':{'version':'1','source_sha':'sha','recipe':'base','environment':{'arch':arch,'ubuntu':'22.04','cann':'9','python':'3.11','simulator':'target'}}}
        a,b=cell('amd64'),cell('arm64')
        self.assertEqual(pair_tag([a,b]),'image:version')
        b['base']['version']='2'
        with self.assertRaises(AssertionError): pair_tag([a,b])

    def test_download_checksum_rejected(self):
        sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'ci'))
        from pipeline import download
        import io
        with tempfile.TemporaryDirectory() as d, patch('urllib.request.urlopen', return_value=io.BytesIO(b'changed')):
            with self.assertRaises(AssertionError): download('https://example.com/wheel',Path(d)/'wheel','0'*64)

    def test_base_dependency_change_rejected(self):
        check=(Path(__file__).parent/'check.py').read_text()
        with patch('subprocess.run'), patch('pathlib.Path.read_text', return_value='numpy==1.26.4\n'), \
                patch('importlib.metadata.version', return_value='2.0.0'):
            with self.assertRaises(AssertionError):
                exec(compile(check, 'dev/check.py', 'exec'), {})

    def test_collection_error_blocks(self):
        e=self.evidence()
        e['events'].append({'nodeid':'broken.py','phase':'collection','outcome':'failed','detail':'ImportError'})
        with self.assertRaises(AssertionError): summarize(e,e['inventory'])

    def test_skipped_module_is_not_complete_collection(self):
        e=self.evidence()
        e['events'].append({'nodeid':'optional.py','phase':'collection','outcome':'skipped','detail':'missing dependency'})
        with self.assertRaises(AssertionError): summarize(e,e['inventory'])

    def test_diagnosis_hash_mismatch_blocks(self):
        e=self.evidence('different failure')
        d={'nodeid':e['inventory'][0],'detail_sha256':'0'*64,'category':'compiler','reason':'Other diagnostic'}
        self.assertFalse(summarize(e,e['inventory'],[d])['publishable'])

if __name__=='__main__': unittest.main()
