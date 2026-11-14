
import csv
import json
import re
from datetime import datetime, timezone

from django.core.management.base import BaseCommand, CommandError

from shop.models import HistoricalOrder

BATCH_SIZE = 500

PRICES = {"lamp": 8000, "stool": 12000}
CUSHION_PRICE = 2500
CHANNELS = {"web", "app", "store"}

RETURNED_VALUES = {
    "0": False,
    "false": False,
    "no": False,
    "1": True,
    "true": True,
    "yes": True,
}

EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+$")

DATE_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2}(\.\d+)?)?(Z|[+-]\d{2}:?\d{2})$"
)


def validate_row(row, seen_ids):
    """Validate a CSV row against eight checks.

    Returns (HistoricalOrder, None) if valid,
    or (None, reason) if invalid.
    """

    # Check 1: duplicate_order_id
    order_id = (row.get("order_id") or "").strip()

    if order_id in seen_ids:
        return None, "duplicate_order_id"

    seen_ids.add(order_id)

    # Check 2: invalid_email
    email = (row.get("email") or "").strip().lower()

    if not EMAIL_PATTERN.match(email):
        return None, "invalid_email"

    # Check 3: unknown_sku
    sku = (row.get("sku") or "").strip().lower()

    if sku not in PRICES:
        return None, "unknown_sku"

    # Check 4: extra_not_allowed
    extra_code = (row.get("extra_code") or "").strip().lower() or None

    if extra_code is not None and not (
        extra_code == "cushion" and sku == "stool"
    ):
        return None, "extra_not_allowed"

    # Check 5: bad_total
    expected_total = PRICES[sku] + (
        CUSHION_PRICE if extra_code else 0
    )

    try:
        total_cents = int((row.get("total_cents") or "").strip())
    except ValueError:
        return None, "bad_total"

    if total_cents != expected_total:
        return None, "bad_total"

    # Check 6: bad_date
    created_raw = (row.get("created_at") or "").strip()

    if not DATE_PATTERN.match(created_raw):
        return None, "bad_date"

    try:
        created_at = datetime.fromisoformat(created_raw)
    except ValueError:
        return None, "bad_date"

    created_at = created_at.astimezone(timezone.utc)

    # Check 7: bad_returned
    returned = RETURNED_VALUES.get(
        (row.get("returned") or "").strip().lower()
    )

    if returned is None:
        return None, "bad_returned"

    # Check 8: bad_channel
    channel = (row.get("channel") or "").strip().lower()

    if channel not in CHANNELS:
        return None, "bad_channel"

    order = HistoricalOrder(
        order_id=order_id,
        email=email,
        sku=sku,
        extra_code=extra_code,
        channel=channel,
        created_at=created_at,
        total_cents=total_cents,
        returned=returned,
    )

    return order, None


def flush_batch(batch):
    """Insert a batch of valid rows into the database.

    Returns the number of created and skipped existing records.
    """

    ids = [order.order_id for order in batch]

    existing = set(
        HistoricalOrder.objects.filter(
            order_id__in=ids
        ).values_list("order_id", flat=True)
    )

    fresh = [
        order for order in batch
        if order.order_id not in existing
    ]

    HistoricalOrder.objects.bulk_create(
        fresh,
        ignore_conflicts=True,
    )

    return len(fresh), len(batch) - len(fresh)


class Command(BaseCommand):
    help = "Import historical orders from a CSV file into HistoricalOrder."

    def add_arguments(self, parser):
        parser.add_argument(
            "path",
            help="Path to the order_history.csv file",
        )

    def handle(self, *args, **options):
        try:
            f = open(
                options["path"],
                newline="",
                encoding="utf-8-sig",
            )
        except OSError as exc:
            raise CommandError(f"Cannot open file: {exc}")

        created = 0
        skipped_existing = 0
        rejected = {}
        seen_ids = set()
        batch = []

        with f:
            for row in csv.DictReader(f):
                order, reason = validate_row(row, seen_ids)

                if reason is not None:
                    rejected[reason] = rejected.get(reason, 0) + 1
                    continue

                batch.append(order)

                if len(batch) >= BATCH_SIZE:
                    created_count, skipped_count = flush_batch(batch)

                    created += created_count
                    skipped_existing += skipped_count
                    batch = []

            if batch:
                created_count, skipped_count = flush_batch(batch)

                created += created_count
                skipped_existing += skipped_count

        self.stdout.write(
            json.dumps({
                "created": created,
                "skippedExisting": skipped_existing,
                "rejected": rejected,
            })
        )
