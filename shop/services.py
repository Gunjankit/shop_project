
import re

from django.db import transaction

from .models import Customer, Item, Order, OutboxMessage


CHARGE_ID_PATTERN = re.compile(r"^ch_[A-Za-z0-9]{8,}$")


class PurchaseError(Exception):
    """Custom exception raised when a purchase business rule fails."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)


def record_purchase(email, sku, external_charge_id, extra_code=None):
    # Step 1: Normalize the email by removing whitespace and converting it to lowercase.
    email = email.strip().lower()

    # Step 2: Validate the external payment charge ID format.
    if not CHARGE_ID_PATTERN.match(external_charge_id):
        raise PurchaseError("invalid_charge_id")

    # Step 3: Execute all database operations within a single atomic transaction.
    # This ensures that either all operations succeed or none of them are committed.
    with transaction.atomic():

        # Rule 1: Handle replay requests.
        # If an order with the same external charge ID already exists,
        # return the existing order without creating another purchase.
        existing = (
            Order.objects.select_related("customer", "item", "extra")
            .filter(external_charge_id=external_charge_id)
            .first()
        )

        if existing is not None:
            existing.replayed = True
            return existing

        # Rule 2: Validate that the requested item exists.
        # select_for_update() locks the item row to prevent concurrent purchases
        # from modifying its stock at the same time.
        try:
            item = Item.objects.select_for_update().get(sku=sku)
        except Item.DoesNotExist:
            raise PurchaseError("unknown_item")

        # Rule 3: Validate the optional extra.
        # The selected extra must belong to the requested item.
        extra = None

        if extra_code is not None:
            extra = item.extras.filter(code=extra_code).first()

            if extra is None:
                raise PurchaseError("extra_not_allowed")

        # Rule 4: Check item availability.
        # A purchase cannot be completed if the item is out of stock.
        if item.remaining == 0:
            raise PurchaseError("sold_out")

        # Rule 5: Prevent duplicate purchases.
        # An existing customer cannot purchase the same item more than once.
        customer = Customer.objects.filter(email=email).first()

        if customer is not None and Order.objects.filter(
            customer=customer, item=item
        ).exists():
            raise PurchaseError("duplicate_purchase")

        # Rule 6: Process a successful purchase.
        # Create a new customer if the email does not already exist.
        customer_is_new = customer is None

        if customer_is_new:
            customer = Customer.objects.create(email=email)

        # Calculate the total price in cents.
        # Include the extra price only when an extra has been selected.
        total = item.price_cents + (extra.price_cents if extra else 0)

        # Create the purchase order with the customer, item, optional extra,
        # external payment charge ID, and calculated total amount.
        order = Order.objects.create(
            customer=customer,
            item=item,
            extra=extra,
            external_charge_id=external_charge_id,
            total_cents=total,
        )

        # Reduce the available stock by one after a successful purchase.
        item.remaining -= 1
        item.save(update_fields=["remaining"])

        # Create an account-created notification if this is a new customer.
        # The outbox pattern ensures notification records are stored
        # within the same database transaction as the purchase.
        if customer_is_new:
            OutboxMessage.objects.create(
                to_email=email,
                kind=OutboxMessage.Kind.ACCOUNT_CREATED,
                external_charge_id=external_charge_id,
            )

        # Create a purchase-recorded notification for every successful purchase.
        OutboxMessage.objects.create(
            to_email=email,
            kind=OutboxMessage.Kind.PURCHASE_RECORDED,
            external_charge_id=external_charge_id,
        )

        # Mark the order as a new purchase rather than a replayed request.
        order.replayed = False

        return order
