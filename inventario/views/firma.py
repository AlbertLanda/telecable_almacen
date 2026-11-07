"""
Pantallas de firma en pantalla de la nota de salida.

La lógica (quién firma, validar el trazo, huella del documento) está en
inventario/services/firma_service.py; aquí solo se arma la página.
La usan el técnico (operaciones/views.py) y el almacenero (abajo).
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render

from inventario.models import DocumentoInventario, FirmaDocumento, TipoDocumento, UserProfile
from inventario.permissions import role_required
from inventario.services import firma_service

RECEPTOR = FirmaDocumento.Rol.RECEPTOR
ALMACEN = FirmaDocumento.Rol.ALMACEN


def _nombre(usuario):
    return (usuario.get_full_name() or usuario.username) if usuario else "-"


def pantalla_firma(request, sal_id, rol, url_lista):
    """
    GET: resumen de la nota + recuadro para firmar.
    POST: guarda la firma con el servicio y vuelve a `url_lista`.
    """
    doc = get_object_or_404(
        DocumentoInventario.objects.select_related("origen", "sede", "solicitante", "retirado_por", "responsable"),
        id=sal_id, tipo=TipoDocumento.SAL,
    )
    if not firma_service.requiere_firma(doc):
        raise Http404("Este documento no requiere firma.")
    firmante = firma_service.firmante_esperado(doc, rol)
    if not firmante or firmante.id != request.user.id:
        raise PermissionDenied("No te corresponde firmar esta nota.")
    if doc.firmas.filter(rol=rol).exists():
        messages.info(request, f"Ya firmaste la nota {doc.numero}.")
        return redirect(url_lista)

    if request.method == "POST":
        try:
            firma_service.registrar_firma(
                doc_id=doc.id,
                usuario=request.user,
                rol=rol,
                trazo=request.POST.get("trazo"),
                sha256_visto=request.POST.get("sha256"),
                acepto=request.POST.get("acepto") == "1",
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"Firmaste la nota {doc.numero}.")
            return redirect(url_lista)

    resp = firma_service.responsable_cuadrilla(doc)
    receptor = firma_service.firmante_receptor(doc)
    if rol == ALMACEN:
        firmas_como = "Almacén (entregué el material)"
    elif resp and resp.id != request.user.id:
        firmas_como = "Crew"
    else:
        firmas_como = "Responsable"
    return render(request, "inventario/firmar_nota.html", {
        "doc": doc,
        "items": doc.items.select_related("producto").order_by("producto__nombre"),
        "firmas_como": firmas_como,
        "responsable": _nombre(resp),
        "retirado_por": _nombre(receptor),
        "sha256": firma_service.hash_documento(doc),
        "aceptacion": firma_service.ACEPTACION[rol],
        "url_lista": url_lista,
    })


def lista_por_firmar(request, rol, url_firmar, url_volver, subtitulo):
    pendientes, firmadas = firma_service.notas_para_firmar(request.user, rol)
    return render(request, "inventario/notas_por_firmar.html", {
        "pendientes": pendientes,
        "firmadas": firmadas[:30],
        "url_firmar": url_firmar,
        "url_volver": url_volver,
        "subtitulo": subtitulo,
        # Solo para técnicos: lo que retiró su cuadrilla a su cargo (solo consulta).
        "equipo": firma_service.notas_de_mi_equipo(request.user)[:50] if rol == RECEPTOR else [],
    })


# ============================================================
# Almacenero: firma las notas que él despachó
# ============================================================
@login_required
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
def almacen_por_firmar(request):
    return lista_por_firmar(
        request, ALMACEN,
        url_firmar="almacen_firmar_nota",
        url_volver="dash_almacen",
        subtitulo="Notas de salida que despachaste a técnicos",
    )


@login_required
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
def almacen_firmar_nota(request, sal_id):
    return pantalla_firma(request, sal_id, ALMACEN, url_lista="almacen_por_firmar")
