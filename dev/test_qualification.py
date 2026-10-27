import importlib.util
import json
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch
from contextlib import ExitStack

from qualification import aggregate, asset_json, cell_status, interruption, partial_cases

spec = importlib.util.spec_from_file_location('resumable_pipeline', Path(__file__).with_name('pipeline.py'))
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


def complete(nodes):
    return {'inventory': nodes, 'exitstatus': 0, 'events': [
        {'nodeid': n, 'phase': phase, 'outcome': 'passed', 'detail': '', 'xfail': None}
        for n in nodes for phase in ('setup', 'call', 'teardown')]}


class Progress(unittest.TestCase):
    def test_kill_is_not_automatically_oom(self):
        self.assertEqual(interruption(137, {})[0], 'interrupted')
        self.assertFalse(interruption(137, {})[2])
        self.assertEqual(interruption(137, {'OOMKilled': True})[0], 'oom')
        self.assertFalse(interruption(137, {'OOMKilled': True})[2])
        self.assertTrue(interruption(124, {}, True)[2])
        self.assertFalse(interruption(124, {}, False)[2])

    def test_partial_cases_keep_failure_blocker_and_unstarted_distinct(self):
        e=complete(['done']);e['active_node']='killed'
        e['events'].append({'nodeid':'bad','phase':'call','outcome':'failed','detail':'assertion'})
        cases=partial_cases(e,['done','bad','killed','later'])
        self.assertEqual([c['status'] for c in cases],['passed','failed','blocked','not-run'])
        self.assertEqual(sum(c['completed'] for c in cases),1)

    def test_incomplete_aggregate_never_passes(self):
        modules=[{'status':'complete','collected':1,'executed':1,'counts':{'passed':1},'publishable':True},
                 {'status':'blocked','collected':1,'executed':0,'retryable':False}]
        s=aggregate(['a','b'],modules)
        self.assertEqual(s['executed'],1)
        self.assertEqual(cell_status(s),'blocked')
        self.assertFalse(s['publishable'])
        modules.append({'status':'not-run'})
        self.assertEqual(cell_status(aggregate(['a','b','c'],modules)),'incomplete')

    def test_legacy_partial_module_list_cannot_claim_full_completion(self):
        s=aggregate(['finished','missing'],[{'status':'complete','collected':1,'executed':1,
                                           'counts':{'passed':1},'publishable':True}])
        self.assertFalse(s['complete'])
        self.assertTrue(s['retryable'])
        self.assertFalse(s['publishable'])
        self.assertEqual(s['suite_status'],'incomplete')

    def test_asset_checksum_rejects_stale_download(self):
        import io
        r={'assets':[{'name':'state.json','id':123,'digest':'sha256:'+'0'*64,'browser_download_url':'https://example.com/state'}]}
        with patch('urllib.request.urlopen',return_value=io.BytesIO(b'{}')) as fetch:
            with self.assertRaisesRegex(AssertionError,'checksum'): asset_json(r,'state.json')
            self.assertIn('?asset=123',fetch.call_args.args[0])

    def test_atomic_plugin_survives_before_session_end(self):
        spec=importlib.util.spec_from_file_location('checkpoint_plugin',Path(__file__).with_name('evidence.py'))
        plugin=importlib.util.module_from_spec(spec);spec.loader.exec_module(plugin)
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{'DEV_EVIDENCE':str(Path(d)/'evidence.json')}):
            plugin.inventory.extend(['a','b'])
            plugin.pytest_runtest_logstart('a',None)
            snapshot=json.loads((Path(d)/'evidence.json').read_text())
            self.assertEqual(snapshot['active_node'],'a')
            self.assertIsNone(snapshot['exitstatus'])
            self.assertEqual(snapshot['inventory'],['a','b'])

    def test_legacy_checkpoint_reuse_checks_exact_cell_and_inventory(self):
        import hashlib, tarfile, shutil
        from qualification import COMPATIBLE_RECIPES
        for wrong_cell in (False, True):
            with self.subTest(wrong_cell=wrong_cell), tempfile.TemporaryDirectory() as d, ExitStack() as stack:
                root=Path(d);work=root/'work';work.mkdir()
                cell={'id':'cell','qualification_recipe':'q'*64}
                module='x/test/unit/a.py';nodes=[module+'::test_a']
                source=root/'source';source.mkdir()
                pipeline.save(source/'identity.json',{'cell_id':'different' if wrong_cell else 'cell',
                                                      'module':module,'qualification_recipe':COMPATIBLE_RECIPES[0]})
                pipeline.save(source/'evidence.json',complete(nodes))
                (source/'junit.xml').write_text('<testsuites/>');(source/'output.log').write_text('passed')
                archive=root/'old.tar.gz'
                with tarfile.open(archive,'w:gz') as tar:
                    for f in source.iterdir():tar.add(f,arcname=f.name)
                asset={'name':pipeline.checkpoint_name(cell,module),'id':1,'browser_download_url':'https://example.com/checkpoint',
                       'digest':'sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()}
                stack.enter_context(patch.object(pipeline,'WORK',work))
                stack.enter_context(patch.object(pipeline,'collect',return_value=nodes))
                stack.enter_context(patch.object(pipeline,'ensure_release'))
                stack.enter_context(patch.object(pipeline,'release',side_effect=lambda tag: {'assets':[asset]} if tag.endswith(COMPATIBLE_RECIPES[0][:12]) else {'assets':[]}))
                stack.enter_context(patch.object(pipeline.base,'run'))
                stack.enter_context(patch.object(pipeline.base,'download',side_effect=lambda url,target,digest:shutil.copyfile(archive,target)))
                execute=stack.enter_context(patch.object(pipeline,'container'))
                stack.enter_context(patch.dict(os.environ,{'GITHUB_RUN_ID':'1','DEV_MAX_MODULES':'0','DEV_BUDGET_MINUTES':'240'}))
                if wrong_cell:
                    with self.assertRaises(AssertionError):pipeline.suite('image',cell,'unused')
                else:
                    result=pipeline.suite('image',cell,'unused')
                    self.assertTrue(result['complete']);self.assertEqual(result['executed'],1)
                    self.assertEqual(result['modules'][0]['evidence_recipe'],COMPATIBLE_RECIPES[0])
                execute.assert_not_called()

    def test_blocked_module_does_not_abort_other_modules(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
            work=Path(d);cell={'id':'cell','qualification_recipe':'q'*64}
            inventory=['x/test/unit/a.py::test_a','x/test/unit/b.py::test_b']
            stack.enter_context(patch.object(pipeline,'WORK',work))
            stack.enter_context(patch.object(pipeline,'RUN_STARTED',time.monotonic()))
            stack.enter_context(patch.object(pipeline,'collect',return_value=inventory))
            stack.enter_context(patch.object(pipeline,'ensure_release'))
            stack.enter_context(patch.object(pipeline,'release',side_effect=lambda tag: {'assets':[]} if tag.endswith('q'*12) else None))
            stack.enter_context(patch.object(pipeline.base,'run'))
            stack.enter_context(patch.dict(os.environ,{'GITHUB_RUN_ID':'1','DEV_MAX_MODULES':'0','DEV_BUDGET_MINUTES':'240'}))
            calls=[]
            def container(image,folder,command,**kwargs):
                node=inventory[len(calls)];calls.append(node)
                oom=len(calls)==1
                pipeline.save(folder/'container-state.json',{'returncode':137 if oom else 0,'state':{'OOMKilled':oom}})
                pipeline.save(folder/'evidence.json',{'inventory':[node],'events':[],'active_node':node,'exitstatus':None} if oom else complete([node]))
                (folder/'output.log').write_text('captured output')
                if not oom:(folder/'junit.xml').write_text('<testsuites/>')
                return 137 if oom else 0
            stack.enter_context(patch.object(pipeline,'container',side_effect=container))
            result=pipeline.suite('image',cell,'unused')
            self.assertEqual(calls,inventory)
            self.assertEqual([m['status'] for m in result['modules']],['blocked','complete'])
            self.assertEqual(result['executed'],1)
            self.assertFalse(result['complete'])
            self.assertFalse(result['retryable'])
            self.assertTrue((work/'progress.json').exists())

if __name__ == '__main__': unittest.main()
