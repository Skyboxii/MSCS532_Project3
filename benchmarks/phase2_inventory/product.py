"""Product record stored in the inventory, plus shared field validators."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from .exceptions import ValidationError

CENT = Decimal("0.01")


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
    return price.quantize(CENT)


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


@dataclass(frozen=True)
class Product:
    """
    An immutable product record. Updates produce a new Product via
    dataclasses.replace(), so callers can never change a field behind the
    indexes' backs and leave them out of sync.  replace() re-runs
    __post_init__, so every updated record is validated too.

    Prices use Decimal rather than float to avoid binary rounding errors in
    currency (e.g. 0.1 + 0.2 != 0.3 with floats).
    """
    sku: str
    name: str
    category: str
    price: Decimal
    quantity: int
    reorder_point: int = 0

    def __post_init__(self):
        # Frozen dataclass: normalized values must be written with object.__setattr__.
        object.__setattr__(self, "sku", to_text(self.sku, "sku"))
        object.__setattr__(self, "name", to_text(self.name, "name"))
        object.__setattr__(self, "category", to_text(self.category, "category"))
        object.__setattr__(self, "price", to_price(self.price))
        to_count(self.quantity, "quantity")
        to_count(self.reorder_point, "reorder_point")

    @property
    def stock_gap(self):
        """Units above the reorder point (<= 0 means reorder now)."""
        return self.quantity - self.reorder_point
