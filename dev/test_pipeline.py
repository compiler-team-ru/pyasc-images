"""Publication is independent of full execution, but never of environment checks."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import ExitStack

spec = importlib.util.spec_from_file_location('development_pipeline', Path(__file__).with_name('pipeline.py'))
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)


class Publication(unittest.TestCase):
    def exercise(self, collection_error=False, qualification=False, suite_error=False):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            work=Path(directory)
            cell={'id':'cell', 'key':'key', 'tag':'ghcr.io/compiler-team-ru/pyasc-sim-dev:tag',
                  'base':{'image':'base@sha256:base','source_sha':'source'},
                  'image':'ghcr.io/compiler-team-ru/pyasc-sim-dev@sha256:dev','digest':'sha256:dev',
                  'suite':{'suite_status':'pending','executed':0}}
            (work/'dev-plan.json').write_text(json.dumps({'pending':[cell],'tag':'release'}))
            stack.enter_context(patch.object(pipeline,'WORK',work))
            stack.enter_context(patch.object(pipeline,'QUALIFY',qualification))
            stack.enter_context(patch.dict(os.environ,{'CELL_KEY':'key','GITHUB_RUN_ID':'test'}))
            stack.enter_context(patch.object(pipeline,'assemble',return_value=('image','sha256:dev')))
            stack.enter_context(patch.object(pipeline.subprocess,'check_output',return_value=json.dumps({'io.pyasc.dev.id':'cell','io.pyasc.dev.base':'base@sha256:base'}).encode()))
            run=stack.enter_context(patch.object(pipeline.base,'run'))
            stack.enter_context(patch.object(pipeline.base,'prepare',return_value={}))
            stack.enter_context(patch.object(pipeline.base,'verify_image',return_value={'status':'pass'}))
            stack.enter_context(patch.object(pipeline.base,'verify_public_digest',return_value={'status':'pass'}))
            stack.enter_context(patch.object(pipeline,'source_tree',return_value='source-hash'))
            def container(image,directory,command):
                directory.mkdir(parents=True,exist_ok=True)
                (directory/'output.log').write_text('{"status":"pass"}\n')
                return 0
            stack.enter_context(patch.object(pipeline,'container',side_effect=container))
            stack.enter_context(patch.object(pipeline,'collect',side_effect=AssertionError('collection failed') if collection_error else None,return_value=['test_a']))
            suite=stack.enter_context(patch.object(pipeline,'suite',return_value={'publishable':False,'suite_status':'failures','executed':1}))
            if suite_error:
                def interrupted(*args):
                    pipeline.save(work/'suite.json', {'inventory':['a'], 'modules':[{'module':'a','executed':1}]})
                    raise AssertionError('interrupted')
                suite.side_effect=interrupted
            if collection_error or suite_error:
                with self.assertRaises(AssertionError): pipeline.build()
            else:
                pipeline.build()
            result=json.loads((work/'dev-result.json').read_text())
            if suite_error:
                self.assertEqual(result['status'],'incomplete')
                self.assertNotIn('suite',result)
                self.assertEqual(result['partial_suite']['modules'][0]['executed'],1)
            elif qualification:
                self.assertEqual(result['status'],'complete')
                self.assertEqual(result['review_status'],'needs-review')
                self.assertEqual(result['suite']['suite_status'],'failures')
                suite.assert_called_once()
            elif collection_error:
                self.assertEqual(result['status'],'blocked')
            else:
                self.assertEqual(result['status'],'published')
                self.assertEqual(result['suite']['suite_status'],'pending')
                self.assertEqual(result['suite']['executed'],0)
                suite.assert_not_called()
            self.assertFalse(any(c.args[:2]==('docker','push') for c in run.call_args_list))

    def test_interrupted_qualification_keeps_partial_progress(self): self.exercise(qualification=True,suite_error=True)

    def test_environment_publication_does_not_claim_full_pass(self): self.exercise()
    def test_collection_failure_still_blocks(self): self.exercise(collection_error=True)
    def test_full_failures_are_reported_without_republishing(self): self.exercise(qualification=True)

    def test_exclusion_report_refresh_does_not_rebuild_published_cells(self):
        with tempfile.TemporaryDirectory() as d, ExitStack() as stack:
            work=Path(d)
            cells=[{'id':'ok','key':'supported','base':{'status':'pass','runner':'ubuntu-24.04'}},
                   {'id':'excluded','key':'unsupported','base':{'status':'pass'},'incompatibility':'No locked wheel'}]
            prior={'cells':[{'id':'ok','status':'published'},{'id':'excluded','status':'blocked'}],
                   'manifests':[{'status':'pass'}]}
            parent={'cells':[{'status':'pass'}]*32}
            stack.enter_context(patch.object(pipeline,'WORK',work))
            stack.enter_context(patch.object(pipeline,'QUALIFY',False))
            stack.enter_context(patch.dict(os.environ,{'INPUT_REPORT':'','INPUT_CELLS':''}))
            stack.enter_context(patch.object(pipeline.base,'pages',return_value=[{'tag_name':'matrix-base','draft':False,'published_at':'date'}]))
            stack.enter_context(patch.object(pipeline,'asset_json',side_effect=lambda r,n: parent if n=='matrix.json' else prior))
            stack.enter_context(patch.object(pipeline,'expand',return_value=cells))
            stack.enter_context(patch.object(pipeline,'recipe',return_value='recipe'))
            stack.enter_context(patch.object(pipeline,'qualification_recipe',return_value='qualification'))
            stack.enter_context(patch.object(pipeline,'release',return_value={'assets':[{'name':'dev-matrix.json'}]}))
            stack.enter_context(patch.object(pipeline,'ensure_release'))
            output=stack.enter_context(patch.object(pipeline.base,'output'))
            pipeline.plan()
            self.assertEqual(json.loads((work/'dev-plan.json').read_text())['pending'],[])
            self.assertIn(('report','true'),[c.args for c in output.call_args_list])

    def test_unselected_qualification_cells_remain_pending(self):
        with tempfile.TemporaryDirectory() as d, patch.object(pipeline,'WORK',Path(d)), \
                patch.object(pipeline,'QUALIFY',True), patch.object(pipeline,'publish_qualification') as publish:
            pipeline.save(Path(d)/'dev-plan.json',{'previous':[], 'pending':[],
                'cells':[{'id':'unselected','base':{'status':'pass'}}]})
            pipeline.finish()
            self.assertEqual(publish.call_args.args[1][0]['status'],'pending')

    def test_new_releases_cannot_take_latest(self):
        with patch.object(pipeline, 'release', return_value=None), patch.object(pipeline.base, 'run') as run, \
                patch.dict(os.environ, {'GITHUB_SHA':'sha'}):
            pipeline.ensure_release('dev-tests-example')
        self.assertIn('--prerelease', run.call_args.args)
        self.assertIn('--latest=false', run.call_args.args)

    def test_qualification_alias_preserves_pending_state(self):
        with tempfile.TemporaryDirectory() as d, patch.object(pipeline, 'WORK', Path(d)), \
                patch.object(pipeline.base, 'run') as run:
            data=dict(base_report='base', base_report_sha256='hash', recipe='image',
                      qualification_recipe='a'*64, tag='dev-report')
            pipeline.publish_qualification(data, [{'status':'pending'}])
            report=json.loads((Path(d)/'qualification.json').read_text())
            self.assertEqual(report['cells'], [{'status':'pending'}])
            self.assertEqual(report, json.loads((Path(d)/('qualification-'+'a'*16+'.json')).read_text()))
            self.assertEqual(run.call_count, 1)

    def test_scheduler_does_not_invalidate_image(self):
        from model import recipe, qualification_recipe
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);(root/'dev').mkdir()
            for name in ('Dockerfile','requirements.lock','torch.lock.json','check.py','evidence.py','pipeline.py','model.py','qualification.py'):
                (root/'dev'/name).write_text(name)
            image,qualification=recipe(root),qualification_recipe(root)
            (root/'dev/pipeline.py').write_text('changed qualification policy')
            self.assertEqual(recipe(root),image)
            self.assertNotEqual(qualification_recipe(root),qualification)
            (root/'dev/requirements.lock').write_text('changed dependencies')
            self.assertNotEqual(recipe(root),image)

if __name__=='__main__': unittest.main()
