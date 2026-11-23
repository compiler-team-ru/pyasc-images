"""Discovery preserves publication recipes and completed image evidence."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pipeline
from matrix import expand
from test_matrix import fixture

OLD_RECIPE, NEW_RECIPE, ENV_RECIPE = 'a' * 64, 'b' * 64, 'e' * 64
OLD_COMMIT, NEW_COMMIT = 'a' * 40, 'b' * 40


def report_for(publication, recipe=OLD_RECIPE):
    lock, _ = fixture()
    cells = [{**c, 'status': 'pass', 'digest': 'sha256:' + c['id']}
             for c in expand(lock, publication, recipe, ENV_RECIPE)]
    return {'version': publication['version'], 'source_sha': publication['source_sha'],
            'recipe': recipe, 'cells': cells,
            'manifests': [{'status': 'pass', 'digest': 'sha256:' + str(i)} for i in range(len(cells) // 2)]}


class DiscoveryTests(unittest.TestCase):
    def plan(self, reports=(), publications=None, version='', rebuild=False, cells='', current_lock=None):
        lock, publication = fixture()
        publications = publications or [publication]
        lock['bootstrap_source'] = publication['source_sha']
        wheel_releases = [{'tag_name': 'wheel-' + p['version'], 'draft': False,
                           'published_at': f'2026-01-{i + 1:02}T00:00:00Z',
                           'assets': [{'name': 'wheel-manifest.json'}], 'data': p}
                          for i, p in enumerate(publications)]
        image_releases = [{'tag_name': 'matrix-' + r['version'] + '-' + r['recipe'][:16],
                           'draft': False, 'published_at': f'2026-02-{i + 1:02}T00:00:00Z',
                           'assets': [{'name': 'matrix.json'}], 'data': r}
                          for i, r in enumerate(reports)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'inputs.lock.json').write_text(json.dumps(current_lock or lock))
            env = {'INPUT_VERSION': version, 'INPUT_CELLS': cells,
                   'INPUT_REBUILD': str(rebuild).lower(), 'GITHUB_SHA': NEW_COMMIT,
                   'GITHUB_OUTPUT': str(root / 'output')}
            previous_dir = Path.cwd()
            try:
                os.chdir(root)
                with patch.object(pipeline, 'ROOT', root), \
                     patch.dict(os.environ, env), \
                     patch.object(pipeline, 'recipe_hash', return_value=NEW_RECIPE), \
                     patch.object(pipeline, 'environment_recipe', return_value=ENV_RECIPE), \
                     patch.object(pipeline, 'pages', side_effect=[image_releases, wheel_releases]), \
                     patch.object(pipeline, 'asset_json', side_effect=lambda r, name: r['data']), \
                     patch.object(pipeline, 'gh', side_effect=lambda path: {'sha': NEW_COMMIT if path.endswith(NEW_RECIPE[:16]) else OLD_COMMIT}), \
                     patch.object(pipeline, 'recorded_recipe', side_effect=lambda commit: (lock, NEW_RECIPE if commit == NEW_COMMIT else OLD_RECIPE, ENV_RECIPE)):
                    pipeline.plan()
                return json.loads((root / 'work/plan.json').read_text())
            finally:
                os.chdir(previous_dir)

    def test_completed_history_survives_controller_changes(self):
        _, publication = fixture()
        report = report_for(publication)
        for _ in range(2):
            result = self.plan([report])
            self.assertEqual(result['pending'], [])
            self.assertFalse(result['needs_report'])
            self.assertEqual(result['recipe'], OLD_RECIPE)
            self.assertEqual(result['recipe_commit'], OLD_COMMIT)
            self.assertEqual(result['previous'], report['cells'])

    def test_partial_history_retries_only_failed_or_missing_cells(self):
        _, publication = fixture()
        report = report_for(publication)
        report['cells'][0]['status'] = 'failed'
        report['cells'][1]['status'] = 'not-run'
        result = self.plan([report])
        self.assertEqual([c['id'] for c in result['pending']], [c['id'] for c in report['cells'][:2]])
        self.assertEqual(result['recipe_commit'], OLD_COMMIT)
        result = self.plan([report], version=publication['version'], cells=report['cells'][0]['key'])
        self.assertEqual(len(result['pending']), 1)

    def test_manifest_repair_does_not_rebuild_images(self):
        _, publication = fixture()
        report = report_for(publication)
        report['manifests'].pop()
        result = self.plan([report])
        self.assertEqual(result['pending'], [])
        self.assertTrue(result['needs_report'])
        self.assertEqual(result['recipe_commit'], OLD_COMMIT)

    def test_new_publication_uses_current_recipe_after_completed_history(self):
        _, publication = fixture()
        newer = copy.deepcopy(publication)
        newer['version'] = '1.1.1.dev2'
        for wheel in newer['wheels'].values():
            wheel['filename'] = wheel['filename'].replace('dev1', 'dev2')
        result = self.plan([report_for(publication)], [publication, newer])
        self.assertEqual(result['publication']['version'], newer['version'])
        self.assertEqual(result['recipe'], NEW_RECIPE)
        self.assertEqual(result['recipe_commit'], NEW_COMMIT)
        self.assertEqual(len(result['pending']), 32)

    def test_new_recipe_requires_explicit_version_and_rebuild(self):
        _, publication = fixture()
        with self.assertRaisesRegex(AssertionError, 'requires an exact version'):
            self.plan(rebuild=True)
        result = self.plan([report_for(publication)], version=publication['version'], rebuild=True)
        self.assertEqual(result['recipe'], NEW_RECIPE)
        self.assertEqual(len(result['pending']), 32)

    def test_repeat_current_recipe_rebuild_skips_verified_cells(self):
        _, publication = fixture()
        result = self.plan([report_for(publication, NEW_RECIPE)],
                           version=publication['version'], rebuild=True)
        self.assertEqual(result['pending'], [])
        self.assertFalse(result['needs_report'])
        self.assertEqual(result['recipe_commit'], NEW_COMMIT)

    def test_latest_explicit_recipe_continues_its_partial_results(self):
        _, publication = fixture()
        newer = report_for(publication, NEW_RECIPE)
        newer['cells'][0]['status'] = 'failed'
        result = self.plan([report_for(publication), newer])
        self.assertEqual(result['recipe_commit'], NEW_COMMIT)
        self.assertEqual([c['id'] for c in result['pending']], [newer['cells'][0]['id']])

    def test_historical_input_lock_is_preserved(self):
        lock, publication = fixture()
        lock['bootstrap_source'] = publication['source_sha']
        lock['python']['3.12']['version'] = '3.12.99'
        result = self.plan([report_for(publication)], current_lock=lock)
        self.assertEqual(result['pending'], [])
        self.assertEqual({c['environment']['python_source']['version'] for c in result['cells']
                          if c['environment']['python'] == '3.12'}, {'3.12.1'})

    def test_mismatched_historical_recipe_or_cells_are_rejected(self):
        _, publication = fixture()
        for field in ('recipe', 'id', 'wheel'):
            with self.subTest(field=field):
                report = report_for(publication)
                if field == 'recipe':
                    report['recipe'] = 'c' * 64
                elif field == 'id':
                    report['cells'][0]['id'] = 'c' * 64
                else:
                    report['cells'][0]['wheel'] = {'sha256': 'wrong'}
                with self.assertRaises(AssertionError):
                    self.plan([report])
