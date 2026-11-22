"""
Indexed binary min-heap (Williams, 1964), with a position map so any item's
priority can be changed or the item removed in O(log n).

Used as the stock-alert index: priority = quantity - reorder_point, so the
root is always the product furthest below (or closest to) its reorder point.

Phase 3 changes:
  * Priorities and items live in two parallel lists instead of one
    [priority, item] list per entry, saving about 70 bytes per product.
  * The position map is a dict instead of the custom HashTable; profiling
    showed two position-map writes on every swap.
  * Sifting moves a "hole" instead of swapping, so each level costs one
    write instead of three.
  * from_items() builds a heap from n entries in O(n) with Floyd's
    bottom-up construction (Floyd, 1964) instead of n pushes (O(n log n)).
"""


class IndexedMinHeap:
    """Min-heap of (priority, item) entries with O(log n) update and remove.
    Ties on priority are broken by item, so ordering is deterministic."""

    def __init__(self):
        self._prio = []                 # priority at each heap position
        self._item = []                 # item at each heap position
        self._pos = {}                  # item -> heap position

    @classmethod
    def from_items(cls, pairs):
        """Build from (item, priority) pairs in O(n). Items must be unique."""
        heap = cls()
        for item, priority in pairs:
            heap._item.append(item)
            heap._prio.append(priority)
        if len(set(heap._item)) != len(heap._item):
            raise KeyError("duplicate items")
        for i in reversed(range(len(heap._item) // 2)):     # internal nodes, bottom up
            heap._sift_down(i)
        heap._pos = {item: i for i, item in enumerate(heap._item)}
        return heap

    # -- heap maintenance -----------------------------------------------------

    def _sift_up(self, i):
        prio, item, pos = self._prio, self._item, self._pos
        p, x = prio[i], item[i]
        while i > 0:
            parent = (i - 1) >> 1
            pp, px = prio[parent], item[parent]
            if p < pp or (p == pp and x < px):
                prio[i], item[i] = pp, px           # move the parent down into the hole
                pos[px] = i
                i = parent
            else:
                break
        prio[i], item[i] = p, x
        pos[x] = i

    def _sift_down(self, i):
        prio, item, pos = self._prio, self._item, self._pos
        n = len(prio)
        p, x = prio[i], item[i]
        while True:
            c = 2 * i + 1
            if c >= n:
                break
            cp, cx = prio[c], item[c]
            if c + 1 < n:                           # pick the smaller child
                rp, rx = prio[c + 1], item[c + 1]
                if rp < cp or (rp == cp and rx < cx):
                    c, cp, cx = c + 1, rp, rx
            if cp < p or (cp == p and cx < x):
                prio[i], item[i] = cp, cx           # move the child up into the hole
                pos[cx] = i
                i = c
            else:
                break
        prio[i], item[i] = p, x
        pos[x] = i

    # -- public API -----------------------------------------------------------

    def push(self, item, priority):
        """Add a new item. O(log n)."""
        if item in self._pos:
            raise KeyError(f"{item!r} is already in the heap")
        self._prio.append(priority)
        self._item.append(item)
        self._sift_up(len(self._prio) - 1)

    def update(self, item, priority):
        """Change an item's priority and restore heap order. O(log n)."""
        i = self._pos[item]
        old = self._prio[i]
        self._prio[i] = priority
        if priority < old:
            self._sift_up(i)
        elif priority > old:
            self._sift_down(i)

    def remove(self, item):
        """Remove an arbitrary item. O(log n)."""
        i = self._pos.pop(item)
        last_p, last_x = self._prio.pop(), self._item.pop()
        if i < len(self._prio):                     # fill the hole with the old last entry
            self._prio[i], self._item[i] = last_p, last_x
            self._pos[last_x] = i
            self._sift_up(i)
            self._sift_down(self._pos[last_x])

    def peek(self):
        """Return (item, priority) with the smallest priority. O(1)."""
        if not self._prio:
            raise IndexError("peek from empty heap")
        return self._item[0], self._prio[0]

    def pop(self):
        """Remove and return (item, priority) with the smallest priority."""
        item, priority = self.peek()
        self.remove(item)
        return item, priority

    def items_at_or_below(self, threshold):
        """
        Yield (item, priority) for every entry with priority <= threshold.
        Subtrees whose root exceeds the threshold are skipped, so the cost is
        O(k) for k results, not O(n).
        """
        prio, item, n = self._prio, self._item, len(self._prio)
        stack = [0] if n else []
        while stack:
            i = stack.pop()
            if prio[i] > threshold:
                continue
            yield item[i], prio[i]
            c = 2 * i + 1
            if c < n:
                stack.append(c)
                if c + 1 < n:
                    stack.append(c + 1)

    def priority_of(self, item):
        return self._prio[self._pos[item]]

    def check_invariants(self):
        """Return a list of problems with heap order or the position map (empty = OK). O(n)."""
        problems = []
        prio, item = self._prio, self._item
        for i in range(1, len(prio)):
            parent = (i - 1) // 2
            if (prio[i], item[i]) < (prio[parent], item[parent]):
                problems.append(f"heap order violated at index {i}")
                break
        if not len(self._pos) == len(prio) == len(item):
            problems.append("position map size does not match heap size")
        for i, x in enumerate(item):
            if self._pos.get(x) != i:
                problems.append(f"position map is wrong for {x!r}")
                break
        return problems

    def __contains__(self, item):
        return item in self._pos

    def __len__(self):
        return len(self._prio)
