"""
Blocked sorted index: an ordered set of unique keys stored as a short list
of sorted blocks.

Phase 3 replacement for the skip list.  Profiling showed the skip list's
pointer-chasing search was the slowest part of every price change, range
query, and name search, and that range queries slowed more than lg n
predicts at 100,000 products, a sign of cache misses.  A B-tree keeps keys
in large sorted nodes for exactly that reason (Bayer & McCreight, 1972;
Rao & Ross, 2000).  This index is a two-level version of the same idea:

    _maxes   [max of block 0, max of block 1, ...]   binary search -> block
    _blocks  [[sorted keys], [sorted keys], ...]     binary search -> slot

Both searches use the C-level bisect module, and a range query is a list
slice of keys stored next to each other in memory.

    search / insert / remove   O(log n + B)   (B = block size; the O(B) part is
                                              one C memmove, not Python work)
    range of k keys            O(log n + k)
    bulk build                 O(n log n)     (one sort, then slicing)
"""

from bisect import bisect_left, bisect_right
from itertools import islice


class SortedKeyList:
    LOAD = 512          # target block size: split above 2*LOAD, merge below LOAD/2

    def __init__(self, keys=(), load=LOAD):
        self._load = load
        ordered = sorted(set(keys))
        self._blocks = [ordered[i:i + load] for i in range(0, len(ordered), load)]
        self._maxes = [b[-1] for b in self._blocks]
        self._len = len(ordered)

    # -- updates --------------------------------------------------------------

    def insert(self, key):
        """Add key; return False if it was already present."""
        maxes, blocks = self._maxes, self._blocks
        if not maxes:
            blocks.append([key])
            maxes.append(key)
            self._len = 1
            return True
        i = bisect_left(maxes, key)
        if i == len(maxes):                     # larger than everything: last block
            i -= 1
        block = blocks[i]
        j = bisect_left(block, key)
        if j < len(block) and block[j] == key:
            return False
        block.insert(j, key)
        maxes[i] = block[-1]
        self._len += 1
        if len(block) > 2 * self._load:         # split an oversized block in half
            half = block[self._load:]
            del block[self._load:]
            maxes[i] = block[-1]
            blocks.insert(i + 1, half)
            maxes.insert(i + 1, half[-1])
        return True

    def remove(self, key):
        """Remove key; raise KeyError if absent."""
        maxes, blocks = self._maxes, self._blocks
        i = bisect_left(maxes, key)
        if i == len(maxes):
            raise KeyError(key)
        block = blocks[i]
        j = bisect_left(block, key)
        if j == len(block) or block[j] != key:
            raise KeyError(key)
        del block[j]
        self._len -= 1
        if not block:
            del blocks[i], maxes[i]
            return
        maxes[i] = block[-1]
        if len(block) < self._load // 2 and len(blocks) > 1:
            self._merge(i)

    def _merge(self, i):
        """Merge a small block into a neighbor so the block count stays O(n / LOAD)."""
        maxes, blocks = self._maxes, self._blocks
        if i == len(blocks) - 1:
            i -= 1                              # merge the last block into its left neighbor
        blocks[i].extend(blocks[i + 1])
        del blocks[i + 1], maxes[i + 1]
        block = blocks[i]
        maxes[i] = block[-1]
        if len(block) > 2 * self._load:
            half = block[self._load:]
            del block[self._load:]
            maxes[i] = block[-1]
            blocks.insert(i + 1, half)
            maxes.insert(i + 1, half[-1])

    # -- queries --------------------------------------------------------------

    def __contains__(self, key):
        i = bisect_left(self._maxes, key)
        if i == len(self._maxes):
            return False
        block = self._blocks[i]
        j = bisect_left(block, key)
        return j < len(block) and block[j] == key

    def range(self, low, high, limit=None):
        """Keys k with low <= k < high, in order (at most `limit` of them). O(log n + k)."""
        out = []
        maxes, blocks = self._maxes, self._blocks
        i = bisect_left(maxes, low)
        if i < len(maxes):
            j = bisect_left(blocks[i], low)
            while i < len(blocks):
                block = blocks[i]
                end = bisect_left(block, high, j)
                out.extend(block[j:end])
                if end < len(block) or (limit is not None and len(out) >= limit):
                    break
                i, j = i + 1, 0
        return out if limit is None else out[:limit]

    def first(self, k):
        """The k smallest keys. O(k)."""
        return list(islice(iter(self), k))

    def __iter__(self):
        for block in self._blocks:
            yield from block

    def __len__(self):
        return self._len

    def check_invariants(self):
        """Blocks sorted, non-empty, in order, maxes correct, size right, no block too big."""
        problems = []
        flat = list(self)
        if any(a >= b for a, b in zip(flat, flat[1:])):
            problems.append("keys are not strictly increasing")
        if len(flat) != self._len:
            problems.append("size counter is wrong")
        if any(not b or b[-1] != m for b, m in zip(self._blocks, self._maxes)):
            problems.append("block maximums are wrong")
        if len(self._blocks) != len(self._maxes):
            problems.append("blocks and maximums differ in length")
        if any(len(b) > 2 * self._load for b in self._blocks):
            problems.append("a block exceeds the maximum size")
        return problems
