"""
Exception hierarchy for the inventory package.

Every error the package raises on purpose is an InventoryError, so callers
(the CLI, a batch loader, an integration) can catch one type.  Each subclass
also inherits from the built-in exception Phase 1 used (KeyError or
ValueError), so existing `except KeyError` / `except ValueError` code keeps
working.
"""


class InventoryError(Exception):
    """Base class for all inventory errors."""

    # KeyError.__str__ wraps the message in quotes; use plain Exception text.
    __str__ = Exception.__str__


class ValidationError(InventoryError, ValueError):
    """A field value is missing, the wrong type, or out of range."""


class ProductNotFoundError(InventoryError, KeyError):
    """No product has the requested SKU."""

    def __init__(self, sku):
        super().__init__(f"SKU {sku!r} not found")
        self.sku = sku


class DuplicateSKUError(InventoryError, KeyError):
    """A product with this SKU already exists."""

    def __init__(self, sku):
        super().__init__(f"SKU {sku!r} already exists")
        self.sku = sku


class InsufficientStockError(InventoryError, ValueError):
    """A stock movement would make the quantity negative."""

    def __init__(self, sku, on_hand, requested):
        super().__init__(f"insufficient stock for {sku}: have {on_hand}, need {requested}")
        self.sku = sku
        self.on_hand = on_hand
        self.requested = requested
