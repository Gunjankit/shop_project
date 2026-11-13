from django.db import migrations


def seed(apps, schema_editor):
    Item = apps.get_model("shop", "Item")
    Extra = apps.get_model("shop", "Extra")

    Item.objects.create(sku="lamp", name="Maple lamp", price_cents=8000, remaining=2)
    stool = Item.objects.create(sku="stool", name="Cedar stool", price_cents=12000, remaining=1)
    Extra.objects.create(item=stool, code="cushion", name="Cushion", price_cents=2500)


class Migration(migrations.Migration):

    dependencies = [
        ("shop", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed, migrations.RunPython.noop),
    ]