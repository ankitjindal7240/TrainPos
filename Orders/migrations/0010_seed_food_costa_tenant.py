from django.db import migrations


def seed_food_costa_tenant(apps, schema_editor):
    Restaurant = apps.get_model("Orders", "Restaurant")
    Vendor = apps.get_model("Orders", "Vendor")
    Customer = apps.get_model("Orders", "Customer")
    Order = apps.get_model("Orders", "Order")
    IncomingEmail = apps.get_model("Orders", "IncomingEmail")

    restaurant, _ = Restaurant.objects.get_or_create(
        slug="food-costa",
        defaults={
            "name": "Food Costa",
            "subscription_status": "ACTIVE",
            "is_active": True,
        },
    )
    if restaurant.subscription_status != "ACTIVE" or not restaurant.is_active:
        restaurant.subscription_status = "ACTIVE"
        restaurant.is_active = True
        restaurant.save(update_fields=["subscription_status", "is_active"])

    Vendor.objects.filter(restaurant__isnull=True).update(restaurant=restaurant)
    Customer.objects.filter(restaurant__isnull=True).update(restaurant=restaurant)
    Order.objects.filter(restaurant__isnull=True).update(restaurant=restaurant)
    IncomingEmail.objects.filter(restaurant__isnull=True).update(restaurant=restaurant)


def unseed_food_costa_tenant(apps, schema_editor):
    # Tenant ownership is intentionally preserved on reverse migration.
    pass


class Migration(migrations.Migration):
    dependencies = [("Orders", "0009_restaurant_restaurantemailconnection_and_more")]

    operations = [migrations.RunPython(seed_food_costa_tenant, unseed_food_costa_tenant)]
