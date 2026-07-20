"""Tests for the multi-pod (sharded) experiment execution.

These pin the invariants the sharding design relies on:
  * generate_config_files is deterministic and index-stable (trial N <-> configN
    on every pod), so pods can pick disjoint trial subsets with no coordination.
  * shard_trial_nums partitions the trial range exactly once with no overlap.
  * merge_partial_result_contents reassembles the shard partials into one header
    + all rows ordered by trial number.
"""
import unittest

from modules.data.parameters import parseRawHyperparameterData
from modules.data.experiment import ExperimentData
from modules.configs import generate_config_files
from modules.runner import shard_trial_nums, merge_partial_result_contents


def _make_experiment():
    """An experiment with two varying integer hyperparameters (3 x 2 = 6 trials)."""
    hyperparameters = parseRawHyperparameterData([
        {"name": "x", "default": "-1", "min": "1", "max": "3", "step": "1", "type": "integer", "useDefault": False},
        {"name": "y", "default": "-1", "min": "1", "max": "2", "step": "1", "type": "integer", "useDefault": False},
    ])
    exp_info = {
        'trialExtraFile': '', 'description': 'd', 'file': 'f',
        'creator': 'u', 'creatorRole': 'user', 'creatorEmail': 'e@e.com',
        'finished': False, 'dumbTextArea': '', 'scatter': False,
        'scatterIndVar': '', 'scatterDepVar': '', 'timeout': 5, 'workers': 1,
        'hyperparameters': hyperparameters, 'name': 'n', 'trialResult': 'out.csv',
        'trialResultLineNumber': 1, 'totalExperimentRuns': 0,
        'sendEmail': False, 'expId': 'abc123', 'configFileFormat': 'ini',
    }
    return ExperimentData(**exp_info)


class TestConfigDeterminism(unittest.TestCase):
    def test_configs_are_index_stable_and_ordered(self):
        """The same input yields the same configN mapping every time (the invariant
        that lets a pod run trial N by looking at config N without coordinating)."""
        exp_a = _make_experiment()
        total_a = generate_config_files(exp_a)

        exp_b = _make_experiment()
        total_b = generate_config_files(exp_b)

        self.assertEqual(total_a, total_b)
        self.assertGreater(total_a, 1)
        # Keys are exactly config0..config{N-1}
        expected_keys = [f'config{i}' for i in range(total_a)]
        self.assertEqual(list(exp_a.configs.keys()), expected_keys)
        # Every config's data is identical across independent runs (index stability)
        for key in expected_keys:
            self.assertEqual(exp_a.configs[key].data, exp_b.configs[key].data)


class TestShardPartitioning(unittest.TestCase):
    def test_partition_is_a_disjoint_cover(self):
        for total in (0, 1, 5, 6, 13, 20):
            for num_shards in (1, 2, 3, 4, 7):
                seen = []
                for shard in range(num_shards):
                    seen.extend(shard_trial_nums(total, shard, num_shards))
                # Every trial appears exactly once, across all shards
                self.assertEqual(sorted(seen), list(range(total)),
                                 msg=f"total={total} numShards={num_shards}")

    def test_extra_shards_get_empty_subsets(self):
        # workers > total: the surplus shards get nothing (and must no-op cleanly)
        subsets = [shard_trial_nums(3, s, 5) for s in range(5)]
        non_empty = [s for s in subsets if s]
        self.assertEqual(sum(len(s) for s in subsets), 3)
        self.assertEqual(len(non_empty), 3)

    def test_round_robin_assignment(self):
        # trial n belongs to shard (n % numShards)
        self.assertEqual(shard_trial_nums(6, 0, 3), [0, 3])
        self.assertEqual(shard_trial_nums(6, 1, 3), [1, 4])
        self.assertEqual(shard_trial_nums(6, 2, 3), [2, 5])


class TestMergePartials(unittest.TestCase):
    def test_merge_orders_rows_and_keeps_one_header(self):
        partials = [
            {"shardIndex": 1, "results": "Experiment Run,out,x\n1,6,1\n3,8,2\n"},
            {"shardIndex": 0, "results": "Experiment Run,out,x\n0,5,1\n2,7,2\n"},
        ]
        rows, header = merge_partial_result_contents(partials)
        self.assertEqual(header, ["Experiment Run", "out", "x"])
        self.assertEqual([r[0] for r in rows], ["0", "1", "2", "3"])
        self.assertEqual(rows[0], ["0", "5", "1"])
        self.assertEqual(rows[3], ["3", "8", "2"])

    def test_merge_tolerates_empty_and_headeronly_partials(self):
        partials = [
            {"shardIndex": 0, "results": ""},
            {"shardIndex": 1, "results": "Experiment Run,out\n1,9\n"},
            {"shardIndex": 2, "results": "Experiment Run,out\n"},  # header only, no rows
        ]
        rows, header = merge_partial_result_contents(partials)
        self.assertEqual(header, ["Experiment Run", "out"])
        self.assertEqual(rows, [["1", "9"]])

    def test_merge_with_no_results_returns_no_header(self):
        rows, header = merge_partial_result_contents([{"shardIndex": 0, "results": ""}])
        self.assertIsNone(header)
        self.assertEqual(rows, [])


if __name__ == '__main__':
    unittest.main(verbosity=2)
