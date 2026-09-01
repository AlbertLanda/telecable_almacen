# Generated manually for proyectos app

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('proyectos', '0007_proyecto_tipo_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='proyecto',
            name='responsable',
            field=models.ForeignKey(
                blank=True,
                help_text='Técnico responsable de recibir y usar los materiales del proyecto, como su mochila semanal.',
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='proyectos_asignados',
                to=settings.AUTH_USER_MODEL,
                verbose_name='Técnico Responsable',
            ),
        ),
        migrations.AlterField(
            model_name='proyecto',
            name='observacion_rechazo',
            field=models.TextField(
                blank=True,
                default='',
                help_text='Feedback del responsable al observar el proyecto.',
            ),
        ),
        migrations.AlterField(
            model_name='proyectomaterial',
            name='observacion_revision',
            field=models.CharField(
                blank=True,
                default='',
                help_text='Nota del responsable sobre este material.',
                max_length=255,
            ),
        ),
    ]
