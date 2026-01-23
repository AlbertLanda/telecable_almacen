from django.db import migrations, models

class Migration(migrations.Migration):
    dependencies = [
        ("inventario", "0004_documentoitem_cantidad_devuelta"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name="documentoitem",
                    name="cantidad_merma",
                    field=models.PositiveIntegerField(default=0),
                ),
            ],
        ),
    ]
