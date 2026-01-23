from django.db import migrations

class Migration(migrations.Migration):

    dependencies = [
        ("inventario", "0007_documentoitem_liq_fields_state_only"),
    ]

    operations = [
        migrations.SeparateDatabaseAndState(
            database_operations=[],
            state_operations=[
                migrations.RemoveField(
                    model_name="documentoitem",
                    name="cantidad_entregada",
                ),
            ],
        ),
    ]
