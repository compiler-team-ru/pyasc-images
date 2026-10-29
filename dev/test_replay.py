import unittest
from replay import failure_nodes


class ReplaySelection(unittest.TestCase):
    def test_only_recorded_failures_in_original_order(self):
        evidence = {'inventory': ['pass', 'bad', 'teardown', 'unstarted'], 'events': [
            {'nodeid': 'pass', 'outcome': 'passed'}, {'nodeid': 'teardown', 'outcome': 'failed'},
            {'nodeid': 'bad', 'outcome': 'failed'}, {'nodeid': 'bad', 'outcome': 'failed'}]}
        self.assertEqual(failure_nodes(evidence), ['bad', 'teardown'])

    def test_unknown_failure_is_rejected(self):
        with self.assertRaises(AssertionError):
            failure_nodes({'inventory': ['a'], 'events': [{'nodeid': 'b', 'outcome': 'failed'}]})


if __name__ == '__main__':
    unittest.main()
