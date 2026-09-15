# Generated manually for inventario app

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0015_documentoinventario_retirado_por'),
        ('proyectos', '0008_alter_proyecto_responsable_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='itemserializado',
            name='proyecto',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='equipos_despachados',
                to='proyectos.proyecto',
            ),
        ),
    ]
