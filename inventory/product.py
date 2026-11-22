"""Product record stored in the inventory, plus shared field validators."""

import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from .exceptions import ValidationError

CENT = Decimal("0.01")

# Flyweight cache: one shared Decimal per distinct price.  A catalog of a
# million products has at most ~100,000 distinct prices between $0.01 and
# $999.99, so products share price objects instead of each holding its own
# 104-byte Decimal.  Bounded so an adversarial stream of unique prices
# cannot grow it forever.
_PRICES = {}
_PRICE_CACHE_LIMIT = 250_000


def to_price(value, field="price"):
    """
    Convert value to a non-negative Decimal with at most two decimal places.

    Accepts str, int, float, or Decimal.  Floats go through str() so 19.99
    becomes Decimal("19.99"), not 19.989999999999998436805981327779591083526611328125.
    Rejects NaN and Infinity, which Decimal would otherwise accept and which
    would silently corrupt the running inventory value.
    """
    if isinstance(value, bool):
        raise ValidationError(f"{field} must be a number, got {value!r}")
    try:
        price = value if isinstance(value, Decimal) else Decimal(str(value).strip())
    except (InvalidOperation, ValueError):
        raise ValidationError(f"{field} must be a number, got {value!r}") from None
    if not price.is_finite():
        raise ValidationError(f"{field} must be finite, got {value!r}")
    if price < 0:
        raise ValidationError(f"{field} cannot be negative, got {value!r}")
    if price != price.quantize(CENT):
        raise ValidationError(f"{field} cannot have fractions of a cent, got {value!r}")
    cents = int(price * 100)
    cached = _PRICES.get(cents)
    if cached is None:
        cached = price.quantize(CENT)
        if len(_PRICES) < _PRICE_CACHE_LIMIT:
            _PRICES[cents] = cached
    return cached


def to_count(value, field):
    """Validate a non-negative whole number (bool is rejected even though it is an int)."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValidationError(f"{field} must be a whole number, got {value!r}")
    if value < 0:
        raise ValidationError(f"{field} cannot be negative, got {value!r}")
    return value


def to_text(value, field):
    """Validate a required, non-blank string and strip surrounding spaces."""
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required")
    return value.strip()


def fold(text):
    """Case-insensitive search key; shares the original string when nothing changes."""
    key = text.casefold()
    return text if key == text else key


_VALIDATORS = {
    "name": lambda v: to_text(v, "name"),
    "category": lambda v: sys.intern(to_text(v, "category")),     # 50 categories, 1M products
    "price": to_price,
    "quantity": lambda v: to_count(v, "quantity"),
    "reorder_point": lambda v: to_count(v, "reorder_point"),
}

# __slots__ (Python 3.10+) removes the per-instance __dict__.
_SLOTS = {"slots": True} if sys.version_info >= (3, 10) else {}


@dataclass(frozen=True, **_SLOTS)
class Product:
    """
    An immutable product record. Updates produce a new Product, so callers
    can never change a field behind the indexes' backs.

    Prices use Decimal rather than float to avoid binary rounding errors in
    currency (e.g. 0.1 + 0.2 != 0.3 with floats).  The indexes use the
    derived integer `cents`, which is computed once here instead of on every
    comparison or total-value update.
    """
    sku: str
    name: str
    category: str
    price: Decimal
    quantity: int
    reorder_point: int = 0
    cents: int = field(init=False, repr=False, compare=False)        # price in whole cents
    name_key: str = field(init=False, repr=False, compare=False)     # casefolded name

    def __post_init__(self):
        put = object.__setattr__        # frozen dataclass: assign through object
        put(self, "sku", to_text(self.sku, "sku"))
        for name, check in _VALIDATORS.items():
            put(self, name, check(getattr(self, name)))
        put(self, "cents", int(self.price * 100))
        put(self, "name_key", fold(self.name))

    def evolve(self, **changes):
        """
        Return a copy with some fields changed, validating ONLY those fields.

        dataclasses.replace() re-runs __post_init__, which re-validates every
        field; profiling showed that was about a quarter of the time of a
        stock update.  The unchanged fields were validated when this record
        was created and cannot have changed since (the record is frozen).
        """
        new = object.__new__(Product)
        put = object.__setattr__
        for name in _FIELDS:
            put(new, name, getattr(self, name))
        for name, value in changes.items():
            check = _VALIDATORS.get(name)
            if check is None:
                raise TypeError(f"cannot change field {name!r}")
            put(new, name, check(value))
        if "price" in changes:
            put(new, "cents", int(new.price * 100))
        if "name" in changes:
            put(new, "name_key", fold(new.name))
        return new

    @property
    def stock_gap(self):
        """Units above the reorder point (<= 0 means reorder now)."""
        return self.quantity - self.reorder_point


_FIELDS = ("sku", "name", "category", "price", "quantity", "reorder_point", "cents", "name_key")
