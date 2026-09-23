from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("Orders", "0012_vendor_parser_type_and_demo_orders")]

    operations = [
        migrations.AddField(
            model_name="vendor",
            name="is_active",
            field=models.BooleanField(default=True),
        ),
    ]
