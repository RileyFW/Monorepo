"""Unit tests for the sharding-related mongo helpers.

These pin the atomic-counter behavior the multi-pod design relies on:
  * increment_exp_value sums deltas ($inc) rather than overwriting, so concurrent
    shard progress writes don't clobber each other.
  * increment_finished_shards hands out a distinct post-increment count each call,
    so exactly one shard observes finishedShards == workers and triggers finalize.

A tiny in-memory fake stands in for the mongo client so the test needs no
running database.
"""
import unittest

from modules.mongo import increment_exp_value, increment_finished_shards

# A syntactically valid 24-hex ObjectId string (ObjectId(expId) is called internally).
FAKE_OID = "0" * 24


class _FakeCollection:
    def __init__(self, doc):
        self.doc = doc

    def _apply(self, update):
        for field, delta in update.get("$inc", {}).items():
            self.doc[field] = self.doc.get(field, 0) + delta
        for field, value in update.get("$set", {}).items():
            self.doc[field] = value

    def update_one(self, _filter, update):
        self._apply(update)

    def find_one_and_update(self, _filter, update, return_document=None):
        self._apply(update)
        return dict(self.doc)


class _FakeDB:
    def __init__(self, collection):
        self.experiments = collection


class _FakeClient:
    def __init__(self, doc):
        self._db = _FakeDB(_FakeCollection(doc))

    def __getitem__(self, _name):
        return self._db


class TestIncrementExpValue(unittest.TestCase):
    def test_increments_accumulate(self):
        client = _FakeClient({"passes": 0})
        for _ in range(3):
            increment_exp_value(FAKE_OID, "passes", 1, client)
        self.assertEqual(client["gladosdb"].experiments.doc["passes"], 3)

    def test_missing_field_starts_from_zero(self):
        client = _FakeClient({})
        increment_exp_value(FAKE_OID, "fails", 2, client)
        self.assertEqual(client["gladosdb"].experiments.doc["fails"], 2)


class TestIncrementFinishedShards(unittest.TestCase):
    def test_returns_distinct_counts_and_worker_total(self):
        client = _FakeClient({"finishedShards": 0, "workers": 3})
        results = [increment_finished_shards(FAKE_OID, client) for _ in range(3)]
        self.assertEqual(results, [(1, 3), (2, 3), (3, 3)])
        # Exactly one call observes the threshold (finishedShards == workers)
        at_threshold = [finished for finished, workers in results if finished >= workers]
        self.assertEqual(at_threshold, [3])

    def test_string_worker_count_still_detects_threshold(self):
        # The frontend number input can persist `workers` as a string; the helper
        # must coerce it so `finished >= workers` compares ints instead of raising
        # TypeError (int vs str) -- which previously swallowed the finalize trigger
        # and left multi-pod experiments stuck un-finished.
        client = _FakeClient({"finishedShards": 0, "workers": "3"})
        results = [increment_finished_shards(FAKE_OID, client) for _ in range(3)]
        self.assertEqual(results, [(1, 3), (2, 3), (3, 3)])
        for finished, workers in results:
            self.assertIsInstance(finished, int)
            self.assertIsInstance(workers, int)
        at_threshold = [finished for finished, workers in results if finished >= workers]
        self.assertEqual(at_threshold, [3])


if __name__ == "__main__":
    unittest.main(verbosity=2)
