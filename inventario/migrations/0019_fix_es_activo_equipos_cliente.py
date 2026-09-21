from django.db import migrations


CODIGOS_EQUIPO_CLIENTE = [
    "TC-ALM-000084",  # Adaptadores Ethernet Starlink
    "TC-ALM-000234",  # Onu liferant CATV
    "TC-ALM-000101",  # TV Box
    "TC-ALM-000306",  # Decoder Digicast
    "TC-ALM-000231",  # Rompemuros 4 antenas - EX220
]


def fix_es_activo(apps, schema_editor):
    """
    Igual que "Routers Starlink" (migración 0018), estos productos estaban
    marcados es_activo=True (Herramienta a devolver) pero en realidad son
    equipo que el técnico instala en casa del cliente y no se devuelve:
    ONU, TV Box, decodificador, el adaptador que acompaña al router
    Starlink, y el equipo de enlace inalámbrico "Rompemuros".

    Al quedar como Herramienta, si no se marcaban como "Bueno"/"Dañado" en
    la liquidación semanal, el sistema no los daba de baja de la mochila
    del técnico y volvían a aparecer pendientes semana tras semana. La ONU
    tenía 4 técnicos afectados al momento de este fix.
    """
    Producto = apps.get_model('inventario', 'Producto')
    Producto.objects.filter(
        codigo_interno__in=CODIGOS_EQUIPO_CLIENTE,
        es_activo=True,
    ).update(es_activo=False)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0018_fix_es_activo_routers_starlink'),
    ]

    operations = [
        migrations.RunPython(fix_es_activo, noop),
    ]
