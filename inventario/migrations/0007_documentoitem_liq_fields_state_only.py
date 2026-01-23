from django.db import migrations, models

class Migration(migrations.Migration):

    dependencies = [
        ("inventario", "0006_merge_20260122_1016"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.AddField(
                    model_name="documentoitem",
                    name="cantidad_entregada",
                    field=models.PositiveIntegerField(default=0),
                ),
                migrations.AddField(
                    model_name="documentoitem",
                    name="cantidad_usada",
                    field=models.PositiveIntegerField(default=0),
                ),
                migrations.AddField(
                    model_name="documentoitem",
                    name="cantidad_merma",
                    field=models.PositiveIntegerField(default=0),
                ),
            ],
        ),
    ]
