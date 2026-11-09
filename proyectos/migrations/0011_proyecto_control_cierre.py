from django.db import migrations, models
from django.db.models import Q


def marcar_cierres_con_evidencia(apps, schema_editor):
    """Solo metadatos: no modifica stock, custodias ni fechas históricas."""
    Proyecto = apps.get_model("proyectos", "Proyecto")
    Documento = apps.get_model("inventario", "DocumentoInventario")
    alias = schema_editor.connection.alias
    con_evidencia = Proyecto.objects.using(alias).filter(
        Q(estado="FINALIZADO")
        | Q(materiales__cantidad_devuelta__gt=0)
        | Q(materiales__cantidad_merma__gt=0)
        | Q(materiales__cantidad_usada__gt=0)
    ).values_list("pk", flat=True)
    Proyecto.objects.using(alias).filter(pk__in=con_evidencia).update(
        cierre_registrado=True, mochila_antigua_conciliada=True,
    )
    referencias = Documento.objects.using(alias).filter(
        tipo="ING", estado="CONFIRMADO", referencia__startswith="RETORNO ",
    ).values_list("referencia", flat=True).distinct()
    for referencia in referencias.iterator():
        Proyecto.objects.using(alias).filter(codigo=referencia[len("RETORNO "):]).update(
            cierre_registrado=True, mochila_antigua_conciliada=True,
        )
    # fin puede ser una fecha estimada, incluso en una obra todavía abierta.
    # Sin evidencia se conservan ambos marcadores en False; los despachos
    # antiguos exigirán revisión explícita antes de liquidar.


class Migration(migrations.Migration):
    dependencies = [
        ("proyectos", "0010_proyecto_liquidado_por"),
        ("inventario", "0021_estado_serial_documento"),
    ]
    operations = [
        migrations.AddField(
            model_name="proyecto", name="cierre_registrado",
            field=models.BooleanField(default=False, editable=False,
                help_text="Existe evidencia de un cierre; la fecha fin estimada no cuenta como cierre."),
        ),
        migrations.AddField(
            model_name="proyecto", name="mochila_antigua_conciliada",
            field=models.BooleanField(default=False, editable=False,
                help_text="La mochila histórica ya fue revisada o existe evidencia de un cierre anterior."),
        ),
        migrations.RunPython(marcar_cierres_con_evidencia, migrations.RunPython.noop),
    ]
