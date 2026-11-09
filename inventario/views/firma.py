"""
Pantallas de firma en pantalla: nota de salida, constancia de devolución,
liquidación, despacho a obra, traslado y pedido entre sedes, orden de compra
y acta de cierre de obra (esta última firma la obra, no un documento de inventario).

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
from types import SimpleNamespace

from inventario.models import DocumentoInventario, FirmaDocumento, TipoDocumento, UserProfile
from inventario.permissions import role_required
from inventario.services import firma_service

RECEPTOR = FirmaDocumento.Rol.RECEPTOR
ALMACEN = FirmaDocumento.Rol.ALMACEN


def _nombre(usuario):
    return (usuario.get_full_name() or usuario.username) if usuario else "-"


# Qué es cada documento y qué representa cada recuadro de firma (para la pantalla de firma).
FS = firma_service
EXPLICACION = {
    FS.NOTA_SALIDA: (
        "Material que el almacén entregó a un técnico. Queda a su cargo (mochila) hasta que lo liquide o lo devuelva.",
        {RECEPTOR: "Quien retira el material", ALMACEN: "Almacén que entrega"},
    ),
    FS.DEVOLUCION: (
        "Material que el técnico devolvió al almacén. Sale de su mochila y vuelve al stock de la sede.",
        {RECEPTOR: "Técnico que devuelve", ALMACEN: "Almacén que recibe"},
    ),
    FS.LIQUIDACION: (
        "Cierre semanal de la mochila del técnico: qué se devolvió en buen estado, qué se perdió (merma) y qué se usó (consumo).",
        {RECEPTOR: "Técnico liquidado (conformidad)", ALMACEN: "Almacén que liquida"},
    ),
    FS.SALIDA_OBRA: (
        "Material despachado para una obra. Queda a cargo del responsable de la obra, aunque lo haya retirado otro técnico.",
        {RECEPTOR: "Responsable de la obra", ALMACEN: "Almacén que despacha"},
    ),
    FS.TRASLADO: (
        "Material que sale de una sede hacia otra. Firma quien lo envía y, al recepcionarlo, quien lo recibe.",
        {ALMACEN: "Almacén que envía", RECEPTOR: "Almacén que recibe"},
    ),
    FS.PEDIDO_SEDES: (
        "Material que una sede pidió a otra. Firma quien hizo el pedido y quien lo atendió.",
        {RECEPTOR: "Sede que pide", ALMACEN: "Sede que atiende"},
    ),
    FS.ORDEN_PROVEEDOR: (
        "Mercadería pedida a un proveedor y recibida en el almacén. La aprobación de compras se firma a mano en la impresión.",
        {RECEPTOR: "Solicitante", ALMACEN: "Recepción conforme"},
    ),
    FS.ACTA_CIERRE: (
        "Conciliación final de los materiales de la obra: entregado, devuelto, merma y consumo.",
        {RECEPTOR: "Responsable técnico", ALMACEN: "Jefatura de almacén (V° B°)"},
    ),
}
ESTADO_TEXTO = {
    "FIRMADO": "Ya firmó",
    "CAMBIADO": "Firmó, pero el documento cambió después",
    "PENDIENTE": "Todavía no firma",
}


def _explicacion(clase, rol, otro_firmante, estado_otro, items, es_liquidacion):
    """Textos de ayuda de la pantalla de firma: qué es, qué rol firmas, la otra parte y totales."""
    texto, roles = EXPLICACION[clase]
    otro_rol = ALMACEN if rol == RECEPTOR else RECEPTOR
    if es_liquidacion:
        totales = {
            "devuelto": sum(int(i.cantidad or 0) for i in items),
            "merma": sum(int(i.cantidad_merma or 0) for i in items),
            "consumo": sum(int(i.cantidad_usada or 0) for i in items),
        }
    else:
        totales = {"cantidad": sum(int(i.cantidad or 0) for i in items)}
    return {
        "explicacion": texto,
        "mi_rol": roles[rol],
        "otra_parte": {
            "rol": roles[otro_rol],
            "nombre": _nombre(otro_firmante) if otro_firmante else None,
            "estado": estado_otro,
            "estado_texto": ESTADO_TEXTO.get(estado_otro, ""),
        },
        "totales": totales,
        "n_items": len(items),
    }


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
    if clase == firma_service.ORDEN_PROVEEDOR:
        return {"nombre": "orden de compra", "url": reverse("req_print", args=[doc.id])}
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
    elif clase == firma_service.ORDEN_PROVEEDOR:
        ingreso = firma_service.salida_del_pedido(doc)
        proveedor = doc.proveedor.razon_social if doc.proveedor_id else (doc.proveedor_manual or "-")
        detalle = [
            f"Proveedor: {proveedor} · Sede: {doc.sede.nombre}",
            f"Solicitó: {_nombre(doc.responsable)} · Recepcionó: {_nombre(ingreso.responsable if ingreso else None)}",
        ]
        firmas_como = "Solicitante" if rol == RECEPTOR else "Recepción conforme"
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
    items = list(doc.items.select_related("producto").order_by("producto__nombre"))
    otro_rol = ALMACEN if rol == RECEPTOR else RECEPTOR
    ayuda = _explicacion(
        clase, rol,
        firma_service.firmante_esperado(doc, otro_rol),
        firma_service.estado_firma(doc, otro_rol),
        items, clase == firma_service.LIQUIDACION,
    )
    return render(request, "inventario/firmar_nota.html", {
        **ayuda,
        "doc": doc,
        "info": info,
        "detalle": detalle,
        "es_liquidacion": clase == firma_service.LIQUIDACION,
        "items": items,
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
    entre_sedes = (firma_service.TRASLADO, firma_service.PEDIDO_SEDES, firma_service.ORDEN_PROVEEDOR)
    pend_rec = [d for d in pend_rec if firma_service.clase_documento(d) in entre_sedes]
    firm_rec = [d for d in firm_rec if firma_service.clase_documento(d) in entre_sedes]
    for doc in pend_alm:
        doc.url_firmar = reverse("almacen_firmar_nota", args=[doc.id])
    for doc in pend_rec:
        doc.url_firmar = reverse("almacen_firmar_recepcion", args=[doc.id])
    return _juntar((pend_alm, firm_alm), (pend_rec, firm_rec), _actas(usuario, ALMACEN, "almacen_firmar_acta"))


def _actas(usuario, rol, url_firmar):
    """Actas de cierre de obra con los mismos atributos que usan las listas de documentos."""
    pendientes, firmadas = firma_service.actas_para_firmar(usuario, rol)
    for p in pendientes + firmadas:
        p.numero, p.fecha = p.codigo, p.fin
        p.info = {"nombre": "acta de cierre de obra", "url": reverse("proyecto_pdf_liquidacion", args=[p.id])}
    for p in pendientes:
        p.url_firmar = reverse(url_firmar, args=[p.id])
    return pendientes, firmadas


def _juntar(*grupos):
    por_fecha = lambda docs: sorted(docs, key=lambda d: d.fecha, reverse=True)
    return (
        por_fecha([d for pend, _ in grupos for d in pend]),
        por_fecha([d for _, firm in grupos for d in firm]),
    )


def documentos_del_tecnico(usuario):
    """Lo que firma un técnico: sus documentos de inventario y las actas de cierre de sus obras."""
    pend, firm = firma_service.notas_para_firmar(usuario, RECEPTOR)
    for doc in pend:
        doc.url_firmar = reverse("tecnico_firmar_nota", args=[doc.id])
    return _juntar((pend, firm), _actas(usuario, RECEPTOR, "tecnico_firmar_acta"))


FILTROS_LISTA = ("todos", "pendientes", "firmados")


def lista_por_firmar(request, rol, url_firmar, url_volver, subtitulo, documentos=None):
    # Filtro de la lista: ?ver=todos (por defecto), ?ver=pendientes o ?ver=firmados
    ver = request.GET.get("ver", "todos")
    if ver not in FILTROS_LISTA:
        ver = "todos"
    pendientes, firmadas = documentos or firma_service.notas_para_firmar(request.user, rol)
    for doc in pendientes + firmadas:
        if not getattr(doc, "info", None):
            doc.info = describir(doc)
        if not getattr(doc, "url_firmar", None) and url_firmar:
            doc.url_firmar = reverse(url_firmar, args=[doc.id])
    return render(request, "inventario/notas_por_firmar.html", {
        "ver": ver,
        "total_pendientes": len(pendientes),
        "total_firmadas": len(firmadas),
        "pendientes": pendientes if ver in ("todos", "pendientes") else [],
        # En "Todos" solo los recientes; en "Firmados" se muestran más.
        "firmadas": (firmadas[:30] if ver == "todos" else firmadas[:200]) if ver in ("todos", "firmados") else [],
        "url_volver": url_volver,
        "subtitulo": subtitulo,
        # Solo para técnicos: lo que retiró su cuadrilla a su cargo (solo consulta).
        "equipo": firma_service.notas_de_mi_equipo(request.user)[:50] if rol == RECEPTOR and ver == "todos" else [],
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
        subtitulo="Salidas que despachaste, devoluciones, liquidaciones, pedidos, órdenes de compra y actas de cierre de obra",
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


# ============================================================
# Acta de cierre de obra
# ============================================================
def pantalla_firma_acta(request, proyecto_id, rol, url_lista):
    """Igual que pantalla_firma, pero lo firmado es la obra (acta de cierre)."""
    from proyectos.models import Proyecto
    proyecto = get_object_or_404(
        Proyecto.objects.select_related("sede", "responsable", "liquidado_por"), id=proyecto_id,
    )
    if not firma_service.acta_requiere_firma(proyecto):
        raise Http404("Esta obra no requiere firma del acta de cierre.")
    firmante = firma_service.acta_firmante(proyecto, rol)
    if not firmante or firmante.id != request.user.id:
        raise PermissionDenied("No te corresponde firmar esta acta.")
    if proyecto.firmas_acta.filter(rol=rol).exists():
        messages.info(request, f"Ya firmaste el acta de cierre de {proyecto.codigo}.")
        return redirect(url_lista)

    if request.method == "POST":
        try:
            firma_service.registrar_firma_acta(
                proyecto_id=proyecto.id,
                usuario=request.user,
                rol=rol,
                trazo=request.POST.get("trazo"),
                sha256_visto=request.POST.get("sha256"),
                acepto=request.POST.get("acepto") == "1",
            )
        except ValidationError as exc:
            messages.error(request, " ".join(exc.messages))
        else:
            messages.success(request, f"Firmaste el acta de cierre de {proyecto.codigo}.")
            return redirect(url_lista)

    # Mismas columnas que la liquidación: devuelto, merma y consumo de cada material entregado.
    items = [
        SimpleNamespace(producto=m.producto, cantidad=m.cantidad_devuelta,
                        cantidad_merma=m.cantidad_merma, cantidad_usada=m.cantidad_usada)
        for m in proyecto.materiales.select_related("producto").order_by("producto__nombre")
        if m.cantidad_entregada > 0
    ]
    otro_rol = ALMACEN if rol == RECEPTOR else RECEPTOR
    ayuda = _explicacion(
        firma_service.ACTA_CIERRE, rol,
        firma_service.acta_firmante(proyecto, otro_rol),
        firma_service.acta_estado_firma(proyecto, otro_rol),
        items, True,
    )
    return render(request, "inventario/firmar_nota.html", {
        **ayuda,
        "doc": SimpleNamespace(numero=proyecto.codigo, fecha=proyecto.fin, sede=proyecto.sede),
        "info": {"nombre": "acta de cierre de obra", "url": reverse("proyecto_pdf_liquidacion", args=[proyecto.id])},
        "detalle": [
            f"Obra: {proyecto.nombre}",
            f"Responsable técnico: {_nombre(proyecto.responsable)} · Liquidó: {_nombre(proyecto.liquidado_por)}",
        ],
        "es_liquidacion": True,
        "items": items,
        "firmas_como": "Jefatura de almacén (V° B°)" if rol == ALMACEN else "Responsable técnico",
        "sha256": firma_service.acta_hash(proyecto),
        "aceptacion": firma_service.ACEPTACION[(firma_service.ACTA_CIERRE, rol)],
        "url_lista": url_lista,
    })


@login_required
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
def almacen_firmar_acta(request, proyecto_id):
    return pantalla_firma_acta(request, proyecto_id, ALMACEN, url_lista="almacen_por_firmar")
