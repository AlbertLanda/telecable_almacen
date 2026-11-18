from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [("inventario", "0023_firma_acta_cierre_obra")]

    operations = [
        migrations.AlterField(
            model_name="documentoinventario",
            name="tipo_requerimiento",
            field=models.CharField(
                max_length=20,
                default="LOCAL",
                choices=[
                    ("LOCAL", "Local (técnico)"),
                    ("PROVEEDOR", "Proveedor"),
                    ("ENTRE_SEDES", "Entre sedes"),
                    ("RETORNO_CENTRAL", "Retorno al almacén central"),
                ],
            ),
        ),
    ]
