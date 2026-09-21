from django.db import migrations


def merge_stock_sede_nula(apps, schema_editor):
    """
    Limpieza de datos: la transferencia de material entre técnicos de una
    misma cuadrilla (proyecto_asignar_cuadrilla en operaciones/views.py)
    creaba la mochila del receptor con StockTecnico.sede=NULL en vez de
    sumarla a su fila existente. Esto se veía como el mismo producto
    "duplicado" en Mi Mochila y quedaba fuera de la liquidación semanal
    (que filtra por sede del almacén). Aquí se fusiona ese stock huérfano
    con la sede operativa real del técnico.
    """
    StockTecnico = apps.get_model('inventario', 'StockTecnico')
    UserProfile = apps.get_model('inventario', 'UserProfile')

    huerfanos = StockTecnico.objects.filter(sede__isnull=True)
    for huerfano in huerfanos:
        try:
            profile = UserProfile.objects.get(user_id=huerfano.tecnico_id)
        except UserProfile.DoesNotExist:
            continue

        sede_destino_id = profile.sede_principal_id or profile.sede_activa_id
        if not sede_destino_id:
            primera = profile.sedes_permitidas.first()
            sede_destino_id = primera.id if primera else None

        if not sede_destino_id:
            continue

        existente = StockTecnico.objects.filter(
            tecnico_id=huerfano.tecnico_id,
            producto_id=huerfano.producto_id,
            sede_id=sede_destino_id,
        ).exclude(pk=huerfano.pk).first()

        if existente:
            existente.cantidad = existente.cantidad + huerfano.cantidad
            existente.save(update_fields=['cantidad'])
            huerfano.delete()
        else:
            huerfano.sede_id = sede_destino_id
            huerfano.save(update_fields=['sede'])


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0016_itemserializado_proyecto'),
    ]

    operations = [
        migrations.RunPython(merge_stock_sede_nula, noop),
    ]
