"""Phase 3 data-structure changes: tombstone-free hash table, blocked sorted index, array heap."""

import random
import unittest

from inventory.hash_table import HashTable


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
