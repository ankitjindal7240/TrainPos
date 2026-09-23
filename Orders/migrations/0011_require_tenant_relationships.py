from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("Orders", "0010_seed_food_costa_tenant")]

    operations = [
        migrations.AlterField(
            model_name="vendor",
            name="restaurant",
            field=models.ForeignKey(
                on_delete=models.PROTECT,
                related_name="vendors",
                to="Orders.restaurant",
            ),
        ),
        migrations.AlterField(
            model_name="customer",
            name="restaurant",
            field=models.ForeignKey(
                on_delete=models.PROTECT,
                related_name="customers",
                to="Orders.restaurant",
            ),
        ),
        migrations.AlterField(
            model_name="order",
            name="restaurant",
            field=models.ForeignKey(
                on_delete=models.PROTECT,
                related_name="orders",
                to="Orders.restaurant",
            ),
        ),
        migrations.AlterField(
            model_name="incomingemail",
            name="restaurant",
            field=models.ForeignKey(
                on_delete=models.PROTECT,
                related_name="incoming_emails",
                to="Orders.restaurant",
            ),
        ),
    ]
