# Generated migration for liquidación system

from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0003_documentoinventario_tipo_requerimiento'),
    ]

    operations = [
        migrations.CreateModel(
            name='LiquidacionSemanal',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('creado_en', models.DateTimeField(auto_now_add=True)),
                ('actualizado_en', models.DateTimeField(auto_now=True)),
                ('fecha_liquidacion', models.DateField()),
                ('semana', models.IntegerField()),
                ('anio', models.IntegerField()),
                ('stock_inicial', models.IntegerField(default=0, validators=[django.core.validators.MinValueValidator(0)])),
                ('stock_final', models.IntegerField(default=0, validators=[django.core.validators.MinValueValidator(0)])),
                ('cantidad_entregada', models.IntegerField(default=0, validators=[django.core.validators.MinValueValidator(0)])),
                ('cantidad_usada', models.IntegerField(default=0, validators=[django.core.validators.MinValueValidator(0)])),
                ('cantidad_devuelta', models.IntegerField(default=0, validators=[django.core.validators.MinValueValidator(0)])),
                ('cantidad_merma', models.IntegerField(default=0, validators=[django.core.validators.MinValueValidator(0)])),
                ('diferencia', models.IntegerField(default=0)),
                ('estado', models.CharField(choices=[('PENDIENTE', 'Pendiente'), ('LIQUIDADO', 'Liquidado'), ('CONSISTENTE', 'Consistente'), ('INCONSISTENTE', 'Inconsistente'), ('REVISAR', 'Requerir Revisión')], default='PENDIENTE', max_length=20)),
                ('observaciones', models.TextField(blank=True, default='')),
                ('liquidado_por', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='auth.user')),
                ('producto', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='liquidaciones', to='inventario.producto')),
                ('sede', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='liquidaciones', to='inventario.sede')),
            ],
            options={
                'verbose_name': 'Liquidación Semanal',
                'verbose_name_plural': 'Liquidaciones Semanales',
                'ordering': ['-fecha_liquidacion', 'sede__nombre', 'producto__nombre'],
            },
        ),
        migrations.CreateModel(
            name='LiquidacionLog',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('creado_en', models.DateTimeField(auto_now_add=True)),
                ('actualizado_en', models.DateTimeField(auto_now=True)),
                ('tipo', models.CharField(choices=[('LIQUIDACION_SEDE', 'Liquidación de Sede'), ('LIQUIDACION_CENTRAL', 'Liquidación Central'), ('CORRECCION', 'Corrección Manual'), ('VERIFICACION', 'Verificación')], max_length=30)),
                ('semana', models.IntegerField()),
                ('anio', models.IntegerField()),
                ('descripcion', models.TextField()),
                ('productos_procesados', models.IntegerField(default=0)),
                ('discrepancias_detectadas', models.IntegerField(default=0)),
                ('sede', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.CASCADE, to='inventario.sede')),
                ('usuario', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='auth.user')),
            ],
            options={
                'verbose_name': 'Log de Liquidación',
                'verbose_name_plural': 'Logs de Liquidación',
                'ordering': ['-creado_en'],
            },
        ),
        migrations.AddIndex(
            model_name='liquidacionsemanal',
            index=models.Index(fields=['fecha_liquidacion'], name='inventario_li_fecha_liquid_idx'),
        ),
        migrations.AddIndex(
            model_name='liquidacionsemanal',
            index=models.Index(fields=['semana', 'anio'], name='inventario_li_semana_anio_idx'),
        ),
        migrations.AddIndex(
            model_name='liquidacionsemanal',
            index=models.Index(fields=['sede'], name='inventario_li_sede_idx'),
        ),
        migrations.AddIndex(
            model_name='liquidacionsemanal',
            index=models.Index(fields=['producto'], name='inventario_li_producto_idx'),
        ),
        migrations.AlterUniqueTogether(
            name='liquidacionsemanal',
            unique_together={('semana', 'anio', 'sede', 'producto')},
        ),
    ]
