"""
Hash table with open addressing, linear probing, and tombstone-free deletion.

Average-case O(1) get/put/remove while the load factor stays bounded
(Knuth, 1998, Section 6.4).

Phase 3 change: deletion uses backward shifting (Knuth's Algorithm R)
instead of tombstones.  In Phase 2 a deleted slot became a tombstone that
every later search had to step over, so a delete-heavy workload made
unsuccessful searches slower and slower until the next rehash.  Backward
shifting moves later entries of the same probe run back into the freed
slot, so the table never contains anything but live entries and empty slots.

The InventoryManager now stores products in Python's built-in dict (the same
open-addressing idea implemented in C); this class remains the from-scratch
implementation used in the tests and the delete-heavy benchmark.
"""


class HashTable:
    """Map hashable, non-None keys to values."""

    MIN_CAPACITY = 8
    MAX_LOAD = 0.6      # grow once live entries exceed 60% of slots

    def __init__(self, capacity=MIN_CAPACITY):
        cap = self.MIN_CAPACITY
        while cap < capacity:
            cap *= 2                    # power of two so we can mask instead of mod
        self._keys = [None] * cap
        self._values = [None] * cap
        self._size = 0

    # -- core probing ---------------------------------------------------------

    def _find(self, key):
        """Return the slot holding key, or the empty slot where it would go."""
        if key is None:
            raise TypeError("HashTable keys cannot be None")
        keys = self._keys
        mask = len(keys) - 1
        i = hash(key) & mask
        while True:
            k = keys[i]
            if k is None or k == key:
                return i
            i = (i + 1) & mask                      # linear probing

    def _grow(self):
        old = zip(self._keys, self._values)
        self._keys = [None] * (len(self._keys) * 2)
        self._values = [None] * len(self._keys)
        self._size = 0
        for k, v in old:
            if k is not None:
                self.put(k, v)

    # -- public API -----------------------------------------------------------

    def put(self, key, value):
        """Insert or replace the value for key. Amortized O(1)."""
        i = self._find(key)
        if self._keys[i] is None:
            self._keys[i] = key
            self._size += 1
        self._values[i] = value
        if self._size > len(self._keys) * self.MAX_LOAD:
            self._grow()

    def get(self, key, default=None):
        """Return the value for key, or default if absent. Average O(1)."""
        i = self._find(key)
        return default if self._keys[i] is None else self._values[i]

    def remove(self, key):
        """
        Delete key and return its value. Raises KeyError if absent.

        Backward-shift deletion: after emptying slot i, walk forward through
        the rest of the probe run.  An entry at slot j whose home slot h is
        NOT cyclically inside (i, j] can legally sit at i (its probe sequence
        passes through i), so it moves back to i and its old slot becomes the
        new hole.  The walk stops at the first empty slot.
        """
        keys, values = self._keys, self._values
        i = self._find(key)
        if keys[i] is None:
            raise KeyError(key)
        value = values[i]
        mask = len(keys) - 1
        j = i
        while True:
            j = (j + 1) & mask
            k = keys[j]
            if k is None:
                break
            h = hash(k) & mask
            # Is h cyclically in (i, j]?  If so, k must stay where it is.
            if (i < j and i < h <= j) or (i > j and (h > i or h <= j)):
                continue
            keys[i], values[i] = k, values[j]       # shift back into the hole
            i = j
        keys[i] = values[i] = None
        self._size -= 1
        return value

    def __getitem__(self, key):
        i = self._find(key)
        if self._keys[i] is None:
            raise KeyError(key)
        return self._values[i]

    __setitem__ = put
    __delitem__ = remove

    def __contains__(self, key):
        return self._keys[self._find(key)] is not None

    def __len__(self):
        return self._size

    def items(self):
        """Yield (key, value) pairs in slot order. O(capacity)."""
        for k, v in zip(self._keys, self._values):
            if k is not None:
                yield k, v

    def __iter__(self):
        for k, _ in self.items():
            yield k

    def values(self):
        for _, v in self.items():
            yield v

    @property
    def load_factor(self):
        return self._size / len(self._keys)

    def check_invariants(self):
        """Every key must be reachable from its home slot without crossing an
        empty slot (the property backward shifting has to preserve)."""
        keys = self._keys
        mask = len(keys) - 1
        problems = []
        for slot, k in enumerate(keys):
            if k is None:
                continue
            i = hash(k) & mask
            while i != slot:
                if keys[i] is None:
                    problems.append(f"{k!r} is cut off from its home slot")
                    break
                i = (i + 1) & mask
        if sum(k is not None for k in keys) != self._size:
            problems.append("size counter does not match the table")
        return problems
