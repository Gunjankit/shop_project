
from unittest.mock import patch

from django.test import TestCase

from .models import Customer, Item, Order, OutboxMessage
from .services import PurchaseError, record_purchase

URL = "/api/purchases/"


def snapshot():
    """Capture the current database state, including stock, customers, orders, and outbox messages."""
    return {
        "lamp": Item.objects.get(sku="lamp").remaining,
        "stool": Item.objects.get(sku="stool").remaining,
        "customers": Customer.objects.count(),
        "orders": Order.objects.count(),
        "outbox": OutboxMessage.objects.count(),
    }


class RecordPurchaseServiceTests(TestCase):

    def test_new_email_creates_customer_and_account_created_message(self):
        order = record_purchase("  Alice@example.com ", "lamp", "ch_lamp00001")

        self.assertFalse(order.replayed)
        self.assertEqual(order.total_cents, 8000)
        self.assertEqual(Customer.objects.count(), 1)
        self.assertEqual(Customer.objects.get().email, "alice@example.com")
        self.assertEqual(Item.objects.get(sku="lamp").remaining, 1)

        messages = list(OutboxMessage.objects.order_by("id"))

        self.assertEqual(
            [message.kind for message in messages],
            ["account_created", "purchase_recorded"],
        )

        for message in messages:
            self.assertEqual(message.to_email, "alice@example.com")
            self.assertEqual(message.external_charge_id, "ch_lamp00001")

    def test_total_includes_extra_when_sold(self):
        order = record_purchase(
            "bob@example.com", "stool", "ch_stool0001", "cushion"
        )

        self.assertEqual(order.total_cents, 14500)
        self.assertEqual(order.extra.code, "cushion")
        self.assertEqual(Item.objects.get(sku="stool").remaining, 0)

    def test_existing_customer_gets_no_account_created_message(self):
        record_purchase("alice@example.com", "lamp", "ch_lamp00001")
        record_purchase("alice@example.com", "stool", "ch_stool0001")

        kinds = list(
            OutboxMessage.objects.order_by("id").values_list("kind", flat=True)
        )

        self.assertEqual(
            kinds,
            ["account_created", "purchase_recorded", "purchase_recorded"],
        )
        self.assertEqual(Customer.objects.count(), 1)

    def test_sold_out_for_new_customer_does_not_save_customer(self):
        record_purchase("bob@example.com", "stool", "ch_stool0001")
        before = snapshot()

        with self.assertRaises(PurchaseError) as ctx:
            record_purchase("charlie@example.com", "stool", "ch_stool0002")

        self.assertEqual(ctx.exception.code, "sold_out")

        self.assertFalse(
            Customer.objects.filter(email="charlie@example.com").exists()
        )
        self.assertEqual(snapshot(), before)

    def test_duplicate_customer_and_sku(self):
        record_purchase("alice@example.com", "lamp", "ch_lamp00001")
        before = snapshot()

        with self.assertRaises(PurchaseError) as ctx:
            record_purchase("ALICE@example.com", "lamp", "ch_lamp00002")

        self.assertEqual(ctx.exception.code, "duplicate_purchase")
        self.assertEqual(snapshot(), before)

    def test_extra_not_sold_for_item(self):
        before = snapshot()

        with self.assertRaises(PurchaseError) as ctx:
            record_purchase(
                "david@example.com", "lamp", "ch_lamp00003", "cushion"
            )

        self.assertEqual(ctx.exception.code, "extra_not_allowed")
        self.assertEqual(snapshot(), before)

    def test_replay_with_different_sku_returns_original_order(self):
        first = record_purchase("alice@example.com", "lamp", "ch_lamp00001")
        before = snapshot()

        again = record_purchase(
            "grace@example.com", "stool", "ch_lamp00001", "cushion"
        )

        self.assertTrue(again.replayed)
        self.assertEqual(again.id, first.id)
        self.assertEqual(again.item.sku, "lamp")
        self.assertEqual(again.customer.email, "alice@example.com")
        self.assertEqual(snapshot(), before)

    def test_replay_works_even_when_item_is_now_sold_out(self):
        first = record_purchase("bob@example.com", "stool", "ch_stool0001")
        again = record_purchase("bob@example.com", "stool", "ch_stool0001")

        self.assertTrue(again.replayed)
        self.assertEqual(again.id, first.id)

    def test_failed_purchases_leave_everything_unchanged(self):
        record_purchase("alice@example.com", "lamp", "ch_lamp00001")
        record_purchase("bob@example.com", "stool", "ch_stool0001")
        before = snapshot()

        cases = [
            ("olivia@example.com", "desk", "ch_fail0001", None, "unknown_item"),
            ("olivia@example.com", "lamp", "ch_fail0002", "cushion", "extra_not_allowed"),
            ("olivia@example.com", "stool", "ch_fail0003", None, "sold_out"),
            ("alice@example.com", "lamp", "ch_fail0004", None, "duplicate_purchase"),
            ("olivia@example.com", "lamp", "bad", None, "invalid_charge_id"),
        ]

        for email, sku, charge, extra, code in cases:
            with self.subTest(code=code):
                with self.assertRaises(PurchaseError) as ctx:
                    record_purchase(email, sku, charge, extra)

                self.assertEqual(ctx.exception.code, code)
                self.assertEqual(snapshot(), before)

    def test_crash_midway_rolls_back_everything(self):
        before = snapshot()

        with patch("shop.services.OutboxMessage") as fake_outbox:
            fake_outbox.objects.create.side_effect = RuntimeError("boom")

            with self.assertRaises(RuntimeError):
                record_purchase("eve@example.com", "lamp", "ch_lamp00009")

        self.assertEqual(snapshot(), before)


class PurchaseApiTests(TestCase):

    def post(self, **body):
        return self.client.post(URL, body, content_type="application/json")

    def test_created_then_replayed(self):
        body = {
            "email": "Alice@example.com",
            "sku": "lamp",
            "externalChargeId": "ch_12345678",
            "extraCode": None,
        }

        first_response = self.post(**body)

        self.assertEqual(first_response.status_code, 201)
        self.assertFalse(first_response.json()["replayed"])

        order = first_response.json()["order"]

        self.assertEqual(order["email"], "alice@example.com")
        self.assertEqual(order["totalCents"], 8000)
        self.assertIsNone(order["extraCode"])

        second_response = self.post(**{**body, "sku": "stool"})

        self.assertEqual(second_response.status_code, 200)
        self.assertTrue(second_response.json()["replayed"])
        self.assertEqual(second_response.json()["order"]["sku"], "lamp")

    def test_error_status_codes(self):
        # First purchase: Bob buys the stool, exhausting its stock.
        self.post(
            email="bob@example.com",
            sku="stool",
            externalChargeId="ch_stool0001",
        )

        # Second purchase: Bob buys the lamp, so a duplicate lamp purchase
        # can be tested independently of the sold-out stool condition.
        self.post(
            email="bob@example.com",
            sku="lamp",
            externalChargeId="ch_lamp0001",
        )

        cases = [
            (
                {
                    "email": "charlie@example.com",
                    "sku": "stool",
                    "externalChargeId": "ch_stool0002",
                },
                409,
                "sold_out",
            ),
            (
                {
                    "email": "bob@example.com",
                    "sku": "lamp",
                    "externalChargeId": "ch_lamp0002",
                },
                409,
                "duplicate_purchase",
            ),
            (
                {
                    "email": "emma@example.com",
                    "sku": "desk",
                    "externalChargeId": "ch_desk00001",
                },
                400,
                "unknown_item",
            ),
            (
                {
                    "email": "emma@example.com",
                    "sku": "lamp",
                    "externalChargeId": "ch_lamp0003",
                    "extraCode": "cushion",
                },
                400,
                "extra_not_allowed",
            ),
            (
                {
                    "email": "emma@example.com",
                    "sku": "lamp",
                    "externalChargeId": "bad",
                },
                400,
                "invalid_charge_id",
            ),
            (
                {},
                400,
                "missing_fields",
            ),
        ]

        for body, http_status, code in cases:
            with self.subTest(code=code):
                response = self.post(**body)

                self.assertEqual(response.status_code, http_status)
                self.assertEqual(response.json(), {"error": code})

    def test_catalog_and_outbox(self):
        self.post(
            email="alice@example.com",
            sku="lamp",
            externalChargeId="ch_lamp00001",
        )

        catalog = self.client.get("/api/catalog/lamp").json()

        self.assertEqual(
            catalog,
            {
                "sku": "lamp",
                "name": "Maple lamp",
                "priceCents": 8000,
                "remaining": 1,
            },
        )

        outbox = self.client.get("/api/outbox").json()

        self.assertEqual(
            outbox,
            [
                {
                    "toEmail": "alice@example.com",
                    "kind": "account_created",
                    "externalChargeId": "ch_lamp00001",
                },
                {
                    "toEmail": "alice@example.com",
                    "kind": "purchase_recorded",
                    "externalChargeId": "ch_lamp00001",
                },
            ],
        )
