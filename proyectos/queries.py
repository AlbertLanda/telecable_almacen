"""Consultas compartidas para la atención de materiales desde almacén."""
from django.db.models import Exists, F, OuterRef, Q

from .models import EstadoProyecto, Proyecto, ProyectoMaterial, ProyectoMaterialPendiente


def proyectos_por_atender():
    """Obras atendibles con alguna entrega o vinculación de catálogo pendiente.

    Se compara cada material por separado: un exceso de un producto nunca
    compensa la falta de otro. Almacén puede atender obras de cualquier sede.
    """
    materiales_pendientes = ProyectoMaterial.objects.filter(
        proyecto_id=OuterRef("pk"), cantidad_planificada__gt=F("cantidad_entregada"),
    )
    catalogo_pendiente = ProyectoMaterialPendiente.objects.filter(
        proyecto_id=OuterRef("pk"), resuelto=False,
    )
    return Proyecto.objects.filter(
        estado__in=[EstadoProyecto.APROBADO, EstadoProyecto.EN_PROCESO],
    ).annotate(
        tiene_pendientes_despacho=Exists(materiales_pendientes),
        tiene_pendientes_catalogo=Exists(catalogo_pendiente),
    ).filter(Q(tiene_pendientes_despacho=True) | Q(tiene_pendientes_catalogo=True))
