from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('proyectos', '0008_alter_proyecto_responsable_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='asignacioncuadrilla',
            name='proyecto',
            field=models.ForeignKey(
                blank=True,
                help_text='Vacío si es un préstamo directo entre técnicos, sin relación a una obra.',
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='transferencias_cuadrilla',
                to='proyectos.proyecto',
            ),
        ),
    ]
