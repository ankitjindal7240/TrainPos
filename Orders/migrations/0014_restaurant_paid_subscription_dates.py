from django.db import migrations, models
from django.utils import timezone


def add_calendar_year(value):
    try:
        return value.replace(year=value.year + 1)
    except ValueError:
        return value.replace(year=value.year + 1, month=2, day=28)


def give_food_costa_a_compatibility_subscription(apps, schema_editor):
    Restaurant = apps.get_model("Orders", "Restaurant")
    restaurant = Restaurant.objects.filter(slug="food-costa").first()
    if not restaurant or restaurant.subscription_status != "ACTIVE":
        return
    if restaurant.subscription_ends_at:
        return
    started_at = timezone.now()
    restaurant.subscription_started_at = started_at
    restaurant.subscription_ends_at = add_calendar_year(started_at)
    restaurant.save(update_fields=["subscription_started_at", "subscription_ends_at"])


class Migration(migrations.Migration):
    dependencies = [("Orders", "0013_vendor_is_active")]

    operations = [
        migrations.AddField(
            model_name="restaurant",
            name="subscription_ends_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="restaurant",
            name="subscription_started_at",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.RunPython(
            give_food_costa_a_compatibility_subscription,
            migrations.RunPython.noop,
        ),
    ]
