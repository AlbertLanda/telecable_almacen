from django.db import migrations


def fix_es_activo(apps, schema_editor):
    """
    "Routers Starlink" estaba marcado como es_activo=True (Herramienta que
    el técnico debe devolver físicamente), pero en realidad se instala en
    casa del cliente igual que una ONU: no se devuelve.

    Al quedar marcado como Herramienta, la liquidación semanal nunca lo
    daba de baja de la mochila del técnico si no se marcaba "Bueno"
    (retorno) o "Dañado" (merma) — así que el mismo equipo seguía
    apareciendo pendiente de liquidar semana tras semana aunque ya
    estuviera instalado en el cliente.

    Se corrige a es_activo=False para que se comporte como equipo
    instalable/consumible: si no se marca como devuelto, la liquidación
    lo pasa a INSTALADO y lo saca de la mochila del técnico.
    """
    Producto = apps.get_model('inventario', 'Producto')
    Producto.objects.filter(
        codigo_interno='TC-ALM-000095',
        nombre__icontains='starlink',
        es_activo=True,
    ).update(es_activo=False)


def noop(apps, schema_editor):
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('inventario', '0017_merge_stock_tecnico_sede_nula'),
    ]

    operations = [
        migrations.RunPython(fix_es_activo, noop),
    ]
