from django.db import migrations, models


def set_known_vendor_parser_types(apps, schema_editor):
    Vendor = apps.get_model("Orders", "Vendor")
    parser_types = {
        "RailRestro": "RAILRESTRO",
        "HomeBytes": "HOMEBYTES",
        "Rajbhog Khana": "RAJBHOG",
        "RailRecipe": "RAILRECIPE",
    }
    for vendor_name, parser_type in parser_types.items():
        Vendor.objects.filter(name=vendor_name).update(parser_type=parser_type)


class Migration(migrations.Migration):
    dependencies = [("Orders", "0011_require_tenant_relationships")]

    operations = [
        migrations.AddField(
            model_name="vendor",
            name="parser_type",
            field=models.CharField(
                choices=[
                    ("RAILRESTRO", "RailRestro"),
                    ("HOMEBYTES", "HomeBytes"),
                    ("RAJBHOG", "Rajbhog Khana"),
                    ("RAILRECIPE", "RailRecipe"),
                ],
                default="RAILRESTRO",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="vendor",
            name="is_demo",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="order",
            name="is_demo",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(set_known_vendor_parser_types, migrations.RunPython.noop),
    ]
