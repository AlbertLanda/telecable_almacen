"""
Pantallas de firma en pantalla: nota de salida, constancia de devolución,
liquidación y despacho a obra.

La lógica (quién firma, validar el trazo, huella del documento) está en
inventario/services/firma_service.py; aquí solo se arma la página.
La usan el técnico (operaciones/views.py) y el almacenero (abajo).
"""
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.http import Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse

from inventario.models import DocumentoInventario, FirmaDocumento, TipoDocumento, UserProfile
from inventario.permissions import role_required
from inventario.services import firma_service

RECEPTOR = FirmaDocumento.Rol.RECEPTOR
ALMACEN = FirmaDocumento.Rol.ALMACEN


def _nombre(usuario):
    return (usuario.get_full_name() or usuario.username) if usuario else "-"


def describir(doc):
    """Texto y enlace de impresión según el documento (para listas y pantalla de firma)."""
    clase = firma_service.clase_documento(doc)
    if clase == firma_service.DEVOLUCION:
        return {"nombre": "constancia de devolución", "url": reverse("devolucion_print", args=[doc.id])}
    if clase == firma_service.LIQUIDACION:
        return {"nombre": "liquidación", "url": reverse("liquidacion_tecnico_print", args=[doc.id])}
    if clase == firma_service.TRASLADO:
        return {"nombre": "traslado entre sedes", "url": reverse("sal_print", args=[doc.id])}
    if clase == firma_service.PEDIDO_SEDES:
        return {"nombre": "pedido entre sedes", "url": reverse("req_print", args=[doc.id])}
    if clase == firma_service.SALIDA_OBRA:
        obra = firma_service.obra_del_documento(doc)
        return {
            "nombre": f"salida a obra {doc.referencia}",
            "url": reverse("proyecto_pdf_salida", args=[obra.id]) if obra else "#",
        }
    return {"nombre": "nota de salida", "url": reverse("sal_print", args=[doc.id])}


def pantalla_firma(request, sal_id, rol, url_lista):
    """
    GET: resumen de la nota + recuadro para firmar.
    POST: guarda la firma con el servicio y vuelve a `url_lista`.
    """
    doc = get_object_or_404(
        DocumentoInventario.objects.select_related(
            "origen", "sede", "sede_destino", "solicitante", "retirado_por", "responsable",
            "entregado_por", "recibido_por",
        ),
        id=sal_id, tipo__in=(TipoDocumento.SAL, TipoDocumento.ING, TipoDocumento.REQ),
    )
    if not firma_service.requiere_firma(doc):
        raise Http404("Este documento no requiere firma.")
    firmante = firma_service.firmante_esperado(doc, rol)
    if not firmante or firmante.id != request.user.id:
        raise PermissionDenied("No te corresponde firmar este documento.")
    info = describir(doc)
    if doc.firmas.filter(rol=rol).exists():
        messages.info(request, f"Ya firmaste la {info['nombre']} {doc.numero}.")
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
            messages.success(request, f"Firmaste la {info['nombre']} {doc.numero}.")
            return redirect(url_lista)

    clase = firma_service.clase_documento(doc)
    if clase == firma_service.TRASLADO:
        detalle = [
            f"De {doc.sede.nombre} a {doc.sede_destino.nombre}",
            f"Envió: {_nombre(doc.responsable)} · Recibió: {_nombre(doc.recibido_por)}",
        ]
        firmas_como = "Almacén que recibe" if rol == RECEPTOR else "Almacén que envía"
    elif clase == firma_service.PEDIDO_SEDES:
        salida = firma_service.salida_del_pedido(doc)
        detalle = [
            f"Pedido de {doc.sede.nombre} a {doc.sede_destino.nombre}",
            f"Pidió: {_nombre(doc.responsable)} · Atendió: {_nombre(salida.responsable if salida else None)}",
        ]
        firmas_como = "Sede solicitante" if rol == RECEPTOR else "Sede que atendió"
    elif clase == firma_service.SALIDA_OBRA:
        obra = firma_service.obra_del_documento(doc)
        detalle = [
            f"Obra: {obra.codigo} - {obra.nombre}" if obra else f"Obra: {doc.referencia}",
            f"Despachado por almacén: {_nombre(doc.responsable)}",
            f"Retirado por: {_nombre(doc.retirado_por or doc.solicitante)} · Responsable de la obra: {_nombre(doc.solicitante)}",
        ]
        firmas_como = "Almacén (entregué el material)" if rol == ALMACEN else "Responsable de la obra"
    elif clase == firma_service.LIQUIDACION:
        detalle = [
            f"Técnico liquidado: {_nombre(doc.solicitante)}",
            f"Liquidado por almacén: {_nombre(doc.responsable)}",
        ]
        firmas_como = "Almacén (liquidé el material)" if rol == ALMACEN else "Técnico (conformidad)"
    elif clase == firma_service.DEVOLUCION:
        detalle = [
            f"Devuelto por: {_nombre(doc.entregado_por)}",
            f"Recibido en almacén por: {_nombre(doc.responsable)}",
        ]
        firmas_como = "Almacén (recibí el material)" if rol == ALMACEN else "Técnico (devolví el material)"
    else:
        resp = firma_service.responsable_cuadrilla(doc)
        receptor = firma_service.firmante_receptor(doc)
        detalle = [
            f"Entregado por almacén: {_nombre(doc.responsable)}",
            f"Retirado por: {_nombre(receptor)} · Responsable de cuadrilla: {_nombre(resp)}",
        ]
        if rol == ALMACEN:
            firmas_como = "Almacén (entregué el material)"
        elif resp and resp.id != request.user.id:
            firmas_como = "Crew"
        else:
            firmas_como = "Responsable"
    return render(request, "inventario/firmar_nota.html", {
        "doc": doc,
        "info": info,
        "detalle": detalle,
        "es_liquidacion": clase == firma_service.LIQUIDACION,
        "items": doc.items.select_related("producto").order_by("producto__nombre"),
        "firmas_como": firmas_como,
        "sha256": firma_service.hash_documento(doc),
        "aceptacion": firma_service.texto_aceptacion(doc, rol),
        "url_lista": url_lista,
    })


def documentos_del_almacenero(usuario):
    """
    Lo que firma un almacenero: como almacén (despachó, recibió una devolución,
    liquidó, envió un traslado, atendió un pedido) y, entre sedes, como la sede
    que recibe el traslado o que hizo el pedido.
    Devuelve (pendientes, firmadas) con `url_firmar` en cada documento.
    """
    pend_alm, firm_alm = firma_service.notas_para_firmar(usuario, ALMACEN)
    pend_rec, firm_rec = firma_service.notas_para_firmar(usuario, RECEPTOR)
    entre_sedes = (firma_service.TRASLADO, firma_service.PEDIDO_SEDES)
    pend_rec = [d for d in pend_rec if firma_service.clase_documento(d) in entre_sedes]
    firm_rec = [d for d in firm_rec if firma_service.clase_documento(d) in entre_sedes]
    for doc in pend_alm:
        doc.url_firmar = reverse("almacen_firmar_nota", args=[doc.id])
    for doc in pend_rec:
        doc.url_firmar = reverse("almacen_firmar_recepcion", args=[doc.id])
    por_fecha = lambda docs: sorted(docs, key=lambda d: d.fecha, reverse=True)
    return por_fecha(pend_alm + pend_rec), por_fecha(firm_alm + firm_rec)


def lista_por_firmar(request, rol, url_firmar, url_volver, subtitulo, documentos=None):
    pendientes, firmadas = documentos or firma_service.notas_para_firmar(request.user, rol)
    for doc in pendientes + firmadas:
        doc.info = describir(doc)
        if not getattr(doc, "url_firmar", None) and url_firmar:
            doc.url_firmar = reverse(url_firmar, args=[doc.id])
    return render(request, "inventario/notas_por_firmar.html", {
        "pendientes": pendientes,
        "firmadas": firmadas[:30],
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
        url_firmar=None,
        url_volver="dash_almacen",
        subtitulo="Salidas que despachaste (a técnicos, obras y otras sedes), devoluciones, liquidaciones y pedidos entre sedes",
        documentos=documentos_del_almacenero(request.user),
    )


@login_required
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
def almacen_firmar_nota(request, sal_id):
    return pantalla_firma(request, sal_id, ALMACEN, url_lista="almacen_por_firmar")


@login_required
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
def almacen_firmar_recepcion(request, sal_id):
    """Entre sedes: firma de la sede que recibe el traslado o que hizo el pedido."""
    return pantalla_firma(request, sal_id, RECEPTOR, url_lista="almacen_por_firmar")
