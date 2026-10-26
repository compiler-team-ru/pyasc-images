"""Development releases cannot replace the stable base-report entry point."""
import importlib.util
from pathlib import Path
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('release_policy', Path(__file__).with_name('releases.py'))
policy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(policy)


class ReleasePolicy(unittest.TestCase):
    def test_only_complete_base_becomes_latest(self):
        releases = [
            dict(id=1, tag_name='dev-tests-cell-recipe', prerelease=False, draft=False, published_at='3', assets=[]),
            dict(id=2, tag_name='matrix-incomplete', prerelease=False, draft=False, published_at='2', assets=[{'name':'matrix.json'}]),
            dict(id=3, tag_name='matrix-complete', prerelease=False, draft=False, published_at='1', assets=[{'name':'matrix.json'}]),
        ]
        def report(release, name):
            return {'cells':[{'status':'pass' if release['id']==3 else 'blocked'}]*32,
                    'manifests':[{'status':'pass'}]*16}
        with patch.object(policy.base,'pages',return_value=releases), \
                patch.object(policy.base,'asset_json',side_effect=report), patch.object(policy,'api') as api:
            policy.reconcile()
        self.assertEqual([(c.args[1].rsplit('/',1)[1],c.args[2]) for c in api.call_args_list],
                         [('1',{'prerelease':True,'make_latest':'false'}),
                          ('3',{'prerelease':False,'make_latest':'true'})])

    def test_no_complete_base_does_not_promote_evidence(self):
        with patch.object(policy.base,'pages',return_value=[]), patch.object(policy,'api') as api:
            with self.assertRaises(RuntimeError): policy.reconcile()
        api.assert_not_called()

if __name__ == '__main__': unittest.main()
