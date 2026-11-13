from django.db import migrations


DEFAULT_CATEGORIES = (
    "Food & Dining",
    "Shopping",
    "Travel",
    "Entertainment",
    "Gaming",
    "Other",
)

DEFAULT_PLATFORMS = (
    "Amazon",
    "Flipkart",
    "Myntra",
    "Swiggy",
    "Zomato",
    "Uber",
    "Other",
)


def create_default_lookups(apps, schema_editor):
    Category = apps.get_model("coupons", "Category")
    Platform = apps.get_model("coupons", "Platform")
    database = schema_editor.connection.alias

    for name in DEFAULT_CATEGORIES:
        Category.objects.using(database).get_or_create(name=name)

    for name in DEFAULT_PLATFORMS:
        Platform.objects.using(database).get_or_create(name=name)


class Migration(migrations.Migration):
    dependencies = [
        ("coupons", "0003_alter_coupon_status"),
    ]

    operations = [
        migrations.RunPython(create_default_lookups, migrations.RunPython.noop),
    ]
