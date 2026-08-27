# Generated manually for proyectos app

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0015_documentoinventario_retirado_por'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('proyectos', '0006_alter_asignacioncuadrilla_options_and_more'),
    ]

    operations = [
        migrations.AddField(
            model_name='proyecto',
            name='tipo',
            field=models.CharField(
                choices=[('PROYECTO', 'Proyecto'), ('AVERIA', 'Avería')],
                default='PROYECTO',
                help_text='Proyecto planificado o avería reportada.',
                max_length=10,
            ),
        ),
        migrations.CreateModel(
            name='ProyectoMaterialPendiente',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('creado_en', models.DateTimeField(auto_now_add=True)),
                ('actualizado_en', models.DateTimeField(auto_now=True)),
                ('nombre_solicitado', models.CharField(max_length=255)),
                ('cantidad_estimada', models.PositiveIntegerField(default=1)),
                ('nota', models.CharField(blank=True, default='', max_length=255)),
                ('resuelto', models.BooleanField(default=False)),
                ('resuelto_en', models.DateTimeField(blank=True, null=True)),
                ('creado_por', models.ForeignKey(on_delete=django.db.models.deletion.PROTECT, related_name='materiales_pendientes_creados', to=settings.AUTH_USER_MODEL)),
                ('material_resultante', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='proyectos.proyectomaterial')),
                ('producto_vinculado', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to='inventario.producto')),
                ('proyecto', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='materiales_pendientes', to='proyectos.proyecto')),
            ],
            options={
                'verbose_name': 'Material pendiente de catálogo',
                'verbose_name_plural': 'Materiales pendientes de catálogo',
                'ordering': ['-creado_en'],
            },
        ),
    ]
