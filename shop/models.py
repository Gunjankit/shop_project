from django.db import models


class Item(models.Model):
    sku = models.CharField(max_length=50, unique=True)
    name = models.CharField(max_length=100)
    price_cents = models.PositiveIntegerField()
    remaining = models.PositiveIntegerField()

    def __str__(self):
        return self.sku


class Extra(models.Model):
    item = models.ForeignKey(Item, on_delete=models.CASCADE, related_name="extras")
    code = models.CharField(max_length=50)
    name = models.CharField(max_length=100)
    price_cents = models.PositiveIntegerField()

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["item", "code"], name="unique_extra_per_item"),
        ]

    def __str__(self):
        return f"{self.item.sku}:{self.code}"


class Customer(models.Model):
    email = models.CharField(max_length=254, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.email


class Order(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name="orders")
    item = models.ForeignKey(Item, on_delete=models.PROTECT, related_name="orders")
    extra = models.ForeignKey(Extra, on_delete=models.PROTECT, null=True, blank=True)
    external_charge_id = models.CharField(max_length=100, unique=True)
    total_cents = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["customer", "item"], name="one_order_per_customer_item"),
        ]


class OutboxMessage(models.Model):
    class Kind(models.TextChoices):
        ACCOUNT_CREATED = "account_created", "Account created"
        PURCHASE_RECORDED = "purchase_recorded", "Purchase recorded"

    to_email = models.CharField(max_length=254)
    kind = models.CharField(max_length=30, choices=Kind.choices)
    external_charge_id = models.CharField(max_length=100)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["id"]