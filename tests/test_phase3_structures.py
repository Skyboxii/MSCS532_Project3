"""Phase 3 data-structure changes: tombstone-free hash table, blocked sorted index, array heap."""

import random
import unittest

from inventory.hash_table import HashTable
from inventory.sorted_index import SortedKeyList


class TestSortedKeyList(unittest.TestCase):
    def test_random_operations_against_sorted_reference(self):
        for load in (1, 2, 4, 64):              # tiny blocks force constant splits and merges
            rng = random.Random(load)
            s, ref = SortedKeyList(load=load), set()
            for step in range(3000):
                k = (rng.randrange(50), f"S{rng.randrange(40)}")
                if rng.random() < 0.45 and ref:
                    k = rng.choice(sorted(ref))
                    s.remove(k)
                    ref.discard(k)
                else:
                    self.assertEqual(s.insert(k), k not in ref)
                    ref.add(k)
                if step % 50 == 0:
                    self.assertEqual(s.check_invariants(), [], (load, step))
                    self.assertEqual(list(s), sorted(ref))
                    lo, hi = sorted((rng.randrange(52), rng.randrange(52)))
                    expected = [x for x in sorted(ref) if (lo, "") <= x < (hi, "")]
                    self.assertEqual(s.range((lo, ""), (hi, "")), expected)
                    self.assertEqual(s.range((lo, ""), (hi, ""), limit=3), expected[:3])
            self.assertEqual(len(s), len(ref))

    def test_bulk_build_and_queries(self):
        keys = [(i % 97, f"K{i:05}") for i in range(10_000)]
        s = SortedKeyList(keys, load=128)
        self.assertEqual(list(s), sorted(keys))
        self.assertEqual(s.check_invariants(), [])
        self.assertEqual(s.first(3), sorted(keys)[:3])
        self.assertIn((5, "K00005"), s)
        self.assertNotIn((5, "nope"), s)
        self.assertEqual(SortedKeyList([(1, "a"), (1, "a")]).first(5), [(1, "a")])   # duplicates collapse

    def test_edges(self):
        s = SortedKeyList(load=2)
        self.assertEqual(s.range((0, ""), (99, "")), [])
        self.assertEqual(s.first(2), [])
        with self.assertRaises(KeyError):
            s.remove((1, "x"))
        for k in [(3, "c"), (1, "a"), (2, "b"), (9, "z"), (5, "e")]:
            s.insert(k)
        with self.assertRaises(KeyError):
            s.remove((10, "past the end"))
        self.assertEqual(s.range((2, ""), (5, "")), [(2, "b"), (3, "c")])     # high is exclusive
        self.assertEqual(s.range((6, ""), (9, "")), [])
        self.assertEqual(s.range((9, ""), (100, "")), [(9, "z")])
        for k in list(s):
            s.remove(k)
        self.assertEqual((len(s), s._blocks, s.check_invariants()), (0, [], []))


class TestBackwardShiftDeletion(unittest.TestCase):
    def test_no_tombstones_after_deletes(self):
        t = HashTable()
        for i in range(1000):
            t.put(i, i)
        for i in range(0, 1000, 2):
            t.remove(i)
        self.assertEqual(sum(k is not None for k in t._keys), 500)    # only live keys remain
        self.assertAlmostEqual(t.load_factor, 500 / len(t._keys))
        self.assertEqual(t.check_invariants(), [])

    def test_colliding_keys_and_wraparound(self):
        # In an 8-slot table, keys 7, 15, 23, ... all hash to slot 7 and wrap to slots 0, 1, ...
        for trial in range(200):
            rng = random.Random(trial)
            t, ref = HashTable(), {}
            for step in range(400):
                k = rng.choice([7 + 8 * rng.randrange(6), rng.randrange(16), -rng.randrange(9)])
                if rng.random() < 0.45 and k in ref:
                    self.assertEqual(t.remove(k), ref.pop(k))
                else:
                    t.put(k, step)
                    ref[k] = step
                self.assertEqual(t.check_invariants(), [])
            self.assertEqual(dict(t.items()), ref)
            self.assertTrue(all((k in t) == (k in ref) for k in range(-10, 60)))

    def test_delete_heavy_churn_keeps_probes_short(self):
        # Constant live size, many insert/delete cycles: the table must not
        # degrade, because there are no tombstones to accumulate.
        t = HashTable(capacity=4096)
        live = list(range(1000))
        for k in live:
            t.put(k, k)
        rng = random.Random(1)
        next_key = 1000
        for _ in range(50_000):
            t.remove(live.pop(rng.randrange(len(live))))
            live.append(next_key)
            t.put(next_key, next_key)
            next_key += 1
        self.assertEqual(len(t), 1000)
        self.assertEqual(len(t._keys), 4096)                 # never had to grow
        self.assertEqual(t.check_invariants(), [])


if __name__ == "__main__":
    unittest.main()
