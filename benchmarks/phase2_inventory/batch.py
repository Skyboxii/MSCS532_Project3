"""
Batch loading: products and stock movements from CSV files.

Files from other systems always contain some bad rows.  Every loader here
validates row by row and returns a BatchResult listing what succeeded and
what failed (with the line number and reason) instead of stopping at the
first error.
"""

import csv
from dataclasses import dataclass, field

from .exceptions import InventoryError, ValidationError
from .product import Product

PRODUCT_COLUMNS = ("sku", "name", "category", "price", "quantity", "reorder_point")
MOVEMENT_COLUMNS = ("sku", "change", "reference")


@dataclass(frozen=True)
class StockMovement:
    """One stock change: positive change = received, negative = sold."""
    sku: str
    change: int
    reference: str = ""
    line: int = 0                       # source line number, for error reports


@dataclass
class BatchResult:
    succeeded: list = field(default_factory=list)
    failed: list = field(default_factory=list)      # (line, message) pairs
    rolled_back: bool = False

    @property
    def ok(self):
        return not self.failed

    def summary(self):
        text = f"{len(self.succeeded)} succeeded, {len(self.failed)} failed"
        return text + (" (batch rolled back)" if self.rolled_back else "")


def parse_int(text, field_name):
    """Strict integer parsing for CSV cells: '12' and '-3' are fine, '1.5' and '' are not."""
    text = (text or "").strip()
    try:
        return int(text)
    except ValueError:
        raise ValidationError(f"{field_name} must be a whole number, got {text!r}") from None


def _rows(path, required):
    """Yield (line_number, row) from a CSV file after checking the header."""
    with open(path, newline="", encoding="utf-8-sig") as f:      # -sig: tolerate Excel's BOM
        reader = csv.DictReader(f)
        missing = [c for c in required if c not in (reader.fieldnames or [])]
        if missing:
            raise ValidationError(f"{path}: missing column(s) {', '.join(missing)}")
        for line, row in enumerate(reader, start=2):              # line 1 is the header
            yield line, row


def read_products(path):
    """Parse a product CSV. Returns (products, failures)."""
    products, failures = [], []
    for line, row in _rows(path, PRODUCT_COLUMNS[:5]):
        try:
            products.append(Product(
                sku=row["sku"], name=row["name"], category=row["category"], price=row["price"],
                quantity=parse_int(row["quantity"], "quantity"),
                reorder_point=parse_int(row.get("reorder_point") or "0", "reorder_point")))
        except InventoryError as e:
            failures.append((line, str(e)))
    return products, failures


def read_movements(path):
    """Parse a stock-movement CSV. Returns (movements, failures)."""
    movements, failures = [], []
    for line, row in _rows(path, MOVEMENT_COLUMNS[:2]):
        try:
            sku = (row["sku"] or "").strip()
            if not sku:
                raise ValidationError("sku is required")
            movements.append(StockMovement(sku, parse_int(row["change"], "change"),
                                           (row.get("reference") or "").strip(), line))
        except InventoryError as e:
            failures.append((line, str(e)))
    return movements, failures


def import_products(inventory, path):
    """Add every valid product in a CSV file; duplicates and bad rows are reported."""
    products, failures = read_products(path)
    result = BatchResult(failed=list(failures))
    for product in products:
        try:
            inventory.add_product(product)
            result.succeeded.append(product.sku)
        except InventoryError as e:
            result.failed.append((None, str(e)))
    return result


def apply_movements(inventory, movements, all_or_nothing=False):
    """
    Apply stock movements in order.

    all_or_nothing=False (default): each movement is its own transaction;
        failures are recorded and the rest still apply, like an integration
        load that rejects bad rows.
    all_or_nothing=True: the whole batch is one transaction; the first
        failure undoes every movement already applied.
    """
    result = BatchResult()
    if not all_or_nothing:
        for m in movements:
            try:
                inventory.adjust_quantity(m.sku, m.change)
                result.succeeded.append(m)
            except InventoryError as e:
                result.failed.append((m.line, str(e)))
        return result
    try:
        with inventory.transaction():
            for m in movements:
                try:
                    inventory.adjust_quantity(m.sku, m.change)
                except InventoryError as e:
                    result.failed.append((m.line, str(e)))
                    raise
                result.succeeded.append(m)
    except InventoryError:
        result.rolled_back = True
        result.succeeded.clear()
    return result
