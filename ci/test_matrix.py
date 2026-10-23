import copy
import unittest
from matrix import expand, merge_pair, pending, validate_publication


def fixture():
    arches = {'amd64': 'x86_64', 'arm64': 'aarch64'}
    lock = {'ubuntu': {u: {a: 'ubuntu@sha256:' + a for a in arches} for u in ('22.04', '24.04')},
            'cann': {c: {a: {'sha256': c + a} for a in arches} for c in ('9.0.0', '9.2.0')},
            'python': {p: {'version': p + '.1'} for p in ('3.9', '3.10', '3.11', '3.12')},
            'simulator': 'Ascend950PR_9599'}
    wheels = {f'cp{p.replace(".", "")}-{a}':
              {'filename': 'pyasc_dev-1.1.1.dev1-cp' + p.replace('.', '') + '-cp' + p.replace('.', '') + '-manylinux_2_28_' + a + '.whl', 'sha256': p + a,
               'url': 'https://files.pythonhosted.org/a.whl', 'llvm': {'revision': 'a' * 40}}
              for p in lock['python'] for a in arches.values()}
    publication = {'schema_version': 1, 'status': 'complete', 'source_repository': 'compiler-team-ru/pyasc',
                   'source_sha': 'a' * 40, 'version': '1.1.1.dev1', 'wheels': wheels,
                   'targets': {k: v['sha256'] for k, v in wheels.items()}}
    return lock, publication


class MatrixTests(unittest.TestCase):
    def cells(self):
        return expand(*fixture(), 'r' * 64, 'e' * 64)

    def test_full_matrix_and_matching_wheels(self):
        cells = self.cells()
        self.assertEqual(len(cells), 32)
        self.assertEqual(len({c['id'] for c in cells}), 32)
        for cell in cells:
            self.assertIn('-cp' + cell['environment']['python'].replace('.', '') + '-', cell['wheel']['filename'])

    def test_unchanged_skips_and_failures_retry(self):
        cells = self.cells()
        previous = [{**c, 'status': 'pass'} for c in cells]
        self.assertEqual(pending(cells, previous), [])
        previous[0]['status'] = 'failed'
        self.assertEqual([c['id'] for c in pending(cells, previous)], [cells[0]['id']])

    def test_partial_retry_preserves_successful_cells(self):
        cells = self.cells()
        previous = [{**c, 'status': 'pass'} for c in cells[:10]]
        self.assertEqual(pending(cells, previous), cells[10:])

    def test_mixed_llvm_rejected(self):
        _, publication = fixture()
        publication['wheels']['cp39-x86_64']['llvm']['revision'] = 'b' * 40
        with self.assertRaises(AssertionError):
            validate_publication(publication)

    def test_wheel_change_does_not_rebuild_environment(self):
        lock, publication = fixture()
        old = expand(lock, publication, 'r' * 64, 'e' * 64)
        publication['source_sha'] = 'b' * 40
        new = expand(lock, publication, 'r' * 64, 'e' * 64)
        self.assertNotEqual(old[0]['id'], new[0]['id'])
        self.assertEqual(old[0]['environment_id'], new[0]['environment_id'])

    def test_bad_checksum_and_partial_publication_rejected(self):
        _, publication = fixture()
        publication['targets']['cp39-x86_64'] = 'bad'
        with self.assertRaises(AssertionError):
            validate_publication(publication)
        _, publication = fixture()
        del publication['wheels']['cp39-x86_64']
        with self.assertRaises(AssertionError):
            validate_publication(publication)

    def test_incompatible_wheels_remain_in_complete_report(self):
        lock, publication = fixture()
        publication['schema_version'] = 2
        publication['incompatible_targets'] = {}
        for arch in ('x86_64', 'aarch64'):
            target = 'cp39-' + arch
            del publication['wheels'][target]
            del publication['targets'][target]
            publication['incompatible_targets'][target] = {
                'source_sha': publication['source_sha'], 'status': 'incompatible',
                'reason': 'dataclass slots requires Python 3.10', 'job_url': 'https://github.com/job', 'log_sha256': 'a' * 64}
        cells = expand(lock, publication, 'r' * 64, 'e' * 64)
        self.assertEqual(len(cells), 32)
        self.assertEqual(len(pending(cells, [])), 24)
        self.assertEqual(sum('incompatibility' in c for c in cells), 8)
        self.assertEqual(pending(cells, [{**c, 'status': 'pass'} for c in pending(cells, [])]), [])

    def test_matrix_subset(self):
        cells = self.cells()
        self.assertEqual(len(pending(cells, [], cells[0]['key'])), 1)
        with self.assertRaises(AssertionError):
            pending(cells, [], 'invalid')

    def test_only_matching_architectures_merge(self):
        pair = [{**c, 'status': 'pass'} for c in self.cells()[:2]]
        merge_pair(*pair)
        for key in ('source_sha', 'version', 'recipe'):
            other = copy.deepcopy(pair)
            other[1][key] = 'different'
            with self.assertRaises(AssertionError):
                merge_pair(*other)
        pair[1]['status'] = 'failed'
        with self.assertRaises(AssertionError):
            merge_pair(*pair)


class DownloadTests(unittest.TestCase):
    def test_download_rejects_changed_bytes(self):
        import io
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pipeline import download
        with tempfile.TemporaryDirectory() as root:
            with patch('pipeline.urllib.request.urlopen', return_value=io.BytesIO(b'changed')):
                with self.assertRaises(AssertionError):
                    download('https://files.pythonhosted.org/a.whl', Path(root) / 'a.whl', 'a' * 64)

    def test_foreign_python_abi_rejected(self):
        _, publication = fixture()
        publication['wheels']['cp39-x86_64']['filename'] = publication['wheels']['cp310-x86_64']['filename']
        with self.assertRaises(AssertionError):
            validate_publication(publication)
