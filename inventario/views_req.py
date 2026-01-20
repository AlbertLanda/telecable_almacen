from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.core.exceptions import ValidationError, PermissionDenied
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.db.models import Q

from inventario.models import (
    Ubicacion,
    DocumentoInventario,
    UserProfile,
    TipoDocumento,
    EstadoDocumento,
    Stock,
    Producto,
    Sede,
    TipoRequerimiento,  # ✅ ahora sí importable (nivel módulo)
)

from inventario.services.req_service import (
    get_or_create_req_borrador,
    add_item_to_req,
    set_item_qty,
    remove_item_from_req,
)

from inventario.services.sal_service import req_to_sal
from inventario.services.lookup_service import buscar_producto_por_code


# --------------------
# Helpers
# --------------------
def _require_roles(user, *roles):
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")
    return profile


def _get_ubicacion_operativa(user):
    profile = getattr(user, "profile", None)
    if not profile:
        raise ValidationError("El usuario no tiene perfil (UserProfile).")

    sede = profile.get_sede_operativa()
    if not sede:
        raise ValidationError("No tienes sede operativa asignada.")

    ubicacion = (
        Ubicacion.objects
        .filter(sede=sede)
        .order_by("nombre")
        .first()
    )

    if not ubicacion:
        raise ValidationError(f"No hay ubicaciones creadas para la sede {sede.nombre}.")

    return ubicacion


def _get_sede_operativa(user):
    profile = getattr(user, "profile", None)
    if not profile:
        raise ValidationError("El usuario no tiene perfil (UserProfile).")
    sede = profile.get_sede_operativa()
    if not sede:
        raise ValidationError("No tienes sede operativa asignada.")
    return sede


def _is_ajax(request) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _producto_codigo(p: Producto) -> str:
    # ✅ en tu models: codigo_interno y barcode
    return (getattr(p, "codigo_interno", "") or getattr(p, "barcode", "") or "").strip()


def _serialize_cart(req: DocumentoInventario):
    items = []
    for it in req.items.select_related("producto").order_by("producto__nombre"):
        p = it.producto
        items.append({
            "producto_id": p.id,
            "nombre": p.nombre,
            "codigo": _producto_codigo(p),
            "cantidad": int(it.cantidad or 0),
            "unidad": getattr(p, "unidad", "") or "",
        })
    return items


def _ensure_req_defaults(req: DocumentoInventario):
    """
    Asegura defaults sin romper nada si el borrador ya existía.
    """
    changed = False

    # si por data antigua viene null/vacío
    if not getattr(req, "tipo_requerimiento", None):
        req.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
        changed = True

    if changed:
        req.save(update_fields=["tipo_requerimiento"])


# --------------------
# Vistas REQ
# --------------------
@login_required
def req_home(request):
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        ubicacion = _get_ubicacion_operativa(request.user)
        sede = _get_sede_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
        return redirect("/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req)

    sedes_central = Sede.objects.filter(tipo=Sede.CENTRAL, activo=True).order_by("nombre")

    return render(
        request,
        "inventario/req_home.html",
        {
            "req": req,
            "ubicacion": ubicacion,
            "sede": sede,
            "items": req.items.select_related("producto").order_by("producto__nombre"),
            "tipo_requerimiento": req.tipo_requerimiento,
            "sedes_central": sedes_central,
        },
    )


@require_POST
@login_required
def req_set_tipo_requerimiento(request):
    """
    POST: setear tipo_requerimiento y (opcional) sede_destino del REQ borrador.
    - PROVEEDOR => sede_destino = None
    - ENTRE_SEDES => requiere sede_destino CENTRAL
    """
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        ubicacion = _get_ubicacion_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": str(e)}, status=403)
        messages.error(request, str(e))
        return redirect("/req/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req)

    tipo = (request.POST.get("tipo_requerimiento") or "").strip().upper()
    sede_destino_id = (request.POST.get("sede_destino_id") or "").strip()

    if tipo not in (TipoRequerimiento.PROVEEDOR, TipoRequerimiento.ENTRE_SEDES):
        msg = "Tipo de requerimiento inválido."
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": msg}, status=400)
        messages.error(request, msg)
        return redirect("/req/")

    if tipo == TipoRequerimiento.PROVEEDOR:
        req.tipo_requerimiento = TipoRequerimiento.PROVEEDOR
        req.sede_destino = None
        req.save(update_fields=["tipo_requerimiento", "sede_destino"])
    else:
        if not sede_destino_id:
            msg = "Selecciona una sede CENTRAL destino."
            if _is_ajax(request):
                return JsonResponse({"ok": False, "error": msg}, status=400)
            messages.error(request, msg)
            return redirect("/req/")

        sede_destino = get_object_or_404(Sede, id=sede_destino_id, activo=True)
        if sede_destino.tipo != Sede.CENTRAL:
            msg = "El destino de un REQ entre sedes debe ser CENTRAL."
            if _is_ajax(request):
                return JsonResponse({"ok": False, "error": msg}, status=400)
            messages.error(request, msg)
            return redirect("/req/")

        req.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
        req.sede_destino = sede_destino
        req.save(update_fields=["tipo_requerimiento", "sede_destino"])

    if _is_ajax(request):
        return JsonResponse({"ok": True, "tipo_requerimiento": req.tipo_requerimiento})

    messages.success(request, "Tipo de requerimiento actualizado.")
    return redirect("/req/")


@login_required
def req_catalogo(request):
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        sede = _get_sede_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=403)

    q = (request.GET.get("q") or "").strip()

    stocks = (
        Stock.objects
        .filter(sede=sede, producto__activo=True, cantidad__gt=0)
        .select_related("producto")
        .order_by("producto__nombre")
    )

    if q:
        stocks = stocks.filter(
            Q(producto__nombre__icontains=q) |
            Q(producto__codigo_interno__icontains=q) |
            Q(producto__barcode__icontains=q)
        )

    stocks = stocks[:80]

    data = []
    for s in stocks:
        p = s.producto
        data.append({
            "producto_id": p.id,
            "nombre": p.nombre,
            "codigo": getattr(p, "codigo_interno", "") or "",
            "disponible": int(s.cantidad or 0),
            "unidad": getattr(p, "unidad", "") or "",
        })

    return JsonResponse({"ok": True, "sede": sede.nombre, "results": data})


@login_required
def req_carrito(request):
    """
    Devuelve el carrito (REQ borrador) en JSON para refrescar sin recargar página.
    GET /req/carrito/
    """
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        ubicacion = _get_ubicacion_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=403)

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req)

    return JsonResponse({
        "ok": True,
        "req_id": req.id,
        "tipo_requerimiento": req.tipo_requerimiento,
        "items": _serialize_cart(req),
    })


@require_POST
@login_required
def req_set_qty(request):
    """
    AJAX: setear cantidad de un item del carrito (REQ BORRADOR)
    POST /req/set-qty/  (producto_id, cantidad)
    """
    if not _is_ajax(request):
        return JsonResponse({"ok": False, "error": "Solo AJAX."}, status=400)

    try:
        profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        ubicacion = _get_ubicacion_operativa(request.user)
        sede = _get_sede_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    except (ValidationError, PermissionDenied) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=403)

    producto_id = request.POST.get("producto_id")
    cantidad_raw = (request.POST.get("cantidad") or "").strip()

    if not producto_id:
        return JsonResponse({"ok": False, "error": "Producto inválido."}, status=400)

    try:
        cantidad = int(cantidad_raw)
    except Exception:
        return JsonResponse({"ok": False, "error": "Cantidad inválida."}, status=400)

    if cantidad <= 0:
        return JsonResponse({"ok": False, "error": "La cantidad debe ser mayor a 0."}, status=400)

    producto = get_object_or_404(Producto, id=producto_id)

    # Seguridad por stock si no es JEFA
    if profile.rol != UserProfile.Rol.JEFA:
        st = Stock.objects.filter(sede=sede, producto=producto).first()
        disponible = int(st.cantidad) if st else 0
        if disponible <= 0:
            return JsonResponse({"ok": False, "error": "Este material no está disponible en tu sede."}, status=400)
        if cantidad > disponible:
            return JsonResponse({"ok": False, "error": f"Solo hay {disponible} disponible(s) en tu sede."}, status=400)

    try:
        item = set_item_qty(user=request.user, req=req, producto=producto, cantidad=cantidad)
    except (ValidationError, PermissionDenied) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)

    return JsonResponse({"ok": True, "producto_id": producto.id, "cantidad": int(item.cantidad)})


@require_POST
@login_required
def req_remove_producto(request):
    """
    AJAX: quitar item del carrito
    POST /req/remove-producto/ (producto_id)
    """
    if not _is_ajax(request):
        return JsonResponse({"ok": False, "error": "Solo AJAX."}, status=400)

    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        ubicacion = _get_ubicacion_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    except (ValidationError, PermissionDenied) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=403)

    producto_id = request.POST.get("producto_id")
    if not producto_id:
        return JsonResponse({"ok": False, "error": "Producto inválido."}, status=400)

    producto = get_object_or_404(Producto, id=producto_id)

    try:
        remove_item_from_req(user=request.user, req=req, producto=producto)
    except (ValidationError, PermissionDenied) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=400)

    return JsonResponse({"ok": True, "producto_id": producto.id})


@require_POST
@login_required
def req_add_producto(request):
    """
    Agregar ítem al REQ por POST usando producto_id + cantidad.
    Si es AJAX => responde JSON (sin redirect).
    """
    try:
        profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        ubicacion = _get_ubicacion_operativa(request.user)
        sede = _get_sede_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": str(e)}, status=403)
        messages.error(request, str(e))
        return redirect("/req/")

    producto_id = request.POST.get("producto_id")
    cantidad_raw = (request.POST.get("cantidad") or "").strip()

    if not producto_id:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": "Producto inválido."}, status=400)
        messages.error(request, "Producto inválido.")
        return redirect("/req/")

    try:
        cantidad = int(cantidad_raw)
    except Exception:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": "Cantidad inválida."}, status=400)
        messages.error(request, "Cantidad inválida.")
        return redirect("/req/")

    if cantidad <= 0:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": "La cantidad debe ser mayor a 0."}, status=400)
        messages.error(request, "La cantidad debe ser mayor a 0.")
        return redirect("/req/")

    producto = get_object_or_404(Producto, id=producto_id)

    # Seguridad: si NO es JEFA, validar disponibilidad en su sede
    if profile.rol != UserProfile.Rol.JEFA:
        st = Stock.objects.filter(sede=sede, producto=producto).first()
        disponible = int(st.cantidad) if st else 0
        if disponible <= 0:
            msg = "Este material no está disponible en tu sede."
            if _is_ajax(request):
                return JsonResponse({"ok": False, "error": msg}, status=400)
            messages.error(request, msg)
            return redirect("/req/")
        if cantidad > disponible:
            msg = f"Solo hay {disponible} disponible(s) en tu sede."
            if _is_ajax(request):
                return JsonResponse({"ok": False, "error": msg}, status=400)
            messages.error(request, msg)
            return redirect("/req/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req)

    try:
        add_item_to_req(user=request.user, req=req, producto=producto, cantidad=cantidad)
    except (ValidationError, PermissionDenied) as e:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        messages.error(request, str(e))
        return redirect("/req/")

    if _is_ajax(request):
        return JsonResponse({
            "ok": True,
            "message": f"Agregado: {producto.nombre} (x{cantidad})",
            "items": _serialize_cart(req),
        })

    messages.success(request, f"Agregado: {producto.nombre} (x{cantidad})")
    return redirect("/req/")


@login_required
def req_add_item(request):
    if request.method != "POST":
        return redirect("/req/")

    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        ubicacion = _get_ubicacion_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
        return redirect("/req/")

    code = (request.POST.get("code") or "").strip()
    if not code:
        messages.error(request, "Escanea o escribe un código válido.")
        return redirect("/req/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req)

    producto = buscar_producto_por_code(code)
    if not producto:
        messages.error(request, f"No se encontró producto con código: {code}")
        return redirect("/req/")

    try:
        add_item_to_req(user=request.user, req=req, producto=producto, cantidad=1)
        messages.success(request, f"Agregado: {producto.nombre}")
    except ValidationError as e:
        messages.error(request, str(e))

    return redirect("/req/")


@require_POST
@login_required
def req_scan_add(request):
    # (Lo puedes mantener porque existe, pero ya no lo usamos en req_home.html)
    try:
        profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/req/")

    code = (request.POST.get("code") or "").strip()
    ubicacion_id = request.POST.get("ubicacion_id")

    if not code or not ubicacion_id:
        messages.error(request, "Código o ubicación inválidos.")
        return redirect("/req/")

    ubicacion = get_object_or_404(Ubicacion, id=ubicacion_id)

    sede_user = profile.get_sede_operativa()
    if profile.rol != UserProfile.Rol.JEFA and ubicacion.sede_id != sede_user.id:
        messages.error(request, "No puedes usar una ubicación de otra sede.")
        return redirect("/req/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req)

    producto = buscar_producto_por_code(code)
    if not producto:
        messages.error(request, f"No se encontró producto con código: {code}")
        return redirect("/req/")

    try:
        add_item_to_req(user=request.user, req=req, producto=producto, cantidad=1)
        messages.success(request, f"Agregado: {producto.nombre}")
    except ValidationError as e:
        messages.error(request, str(e))

    return redirect("/req/")


@require_POST
@login_required
def req_enviar(request, req_id: int):
    req = get_object_or_404(DocumentoInventario, id=req_id, tipo=TipoDocumento.REQ)

    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/req/")

    if req.responsable_id != request.user.id:
        messages.error(request, "No puedes enviar un REQ que no es tuyo.")
        return redirect("/req/")

    # ✅ Validación extra: si es ENTRE_SEDES, debe tener sede_destino CENTRAL
    if getattr(req, "tipo_requerimiento", None) == TipoRequerimiento.ENTRE_SEDES and not req.sede_destino_id:
        messages.error(request, "Este REQ es ENTRE SEDES: selecciona la sede CENTRAL destino antes de enviar.")
        return redirect("/req/")

    try:
        req.enviar_req()
        messages.success(request, f"REQ enviado: {req.numero}")
    except ValidationError as e:
        messages.error(request, str(e))

    return redirect("/req/")


@require_POST
@login_required
def req_convert_to_sal(request, req_id: int):
    req = get_object_or_404(DocumentoInventario, id=req_id, tipo=TipoDocumento.REQ)

    try:
        profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/")

    if req.estado != EstadoDocumento.REQ_PENDIENTE:
        messages.error(request, "El REQ debe estar en estado PENDIENTE para generar SAL.")
        return redirect("/")

    sede_user = profile.get_sede_operativa()
    if profile.rol != UserProfile.Rol.JEFA and req.sede_id != sede_user.id:
        messages.error(request, "No puedes atender REQ de otra sede.")
        return redirect("/")

    try:
        sal = req_to_sal(user=request.user, req=req, responsable=request.user)
        messages.success(request, f"SAL creada correctamente: {sal.numero or sal.id}")
        return redirect(f"/sal/{sal.id}/")
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
        return redirect("/")


# --------------------
# IMPRESIÓN REQ (2 formatos)
# --------------------
@login_required
def req_print(request, req_id: int):
    """
    Imprime REQ con template según tipo_requerimiento:
    - PROVEEDOR     => inventario/req_print_proveedor.html
    - ENTRE_SEDES   => inventario/req_print_entre_sedes.html
    """
    req = get_object_or_404(
        DocumentoInventario.objects.select_related("sede", "sede_destino", "responsable", "ubicacion"),
        id=req_id,
        tipo=TipoDocumento.REQ,
    )

    items = req.items.select_related("producto").order_by("producto__nombre")

    try:
        profile = getattr(request.user, "profile", None)
        if not profile:
            raise PermissionDenied("Usuario sin perfil (UserProfile).")

        # JEFA/ADMIN: ven todo
        if profile.rol in (UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN):
            pass
        # SOLICITANTE: solo sus reqs
        elif profile.rol == UserProfile.Rol.SOLICITANTE:
            if req.responsable_id != request.user.id:
                raise PermissionDenied("No puedes imprimir un REQ que no es tuyo.")
        # ALMACEN: solo reqs de su sede
        elif profile.rol == UserProfile.Rol.ALMACEN:
            sede = profile.get_sede_operativa()
            if sede and req.sede_id != sede.id:
                raise PermissionDenied("No puedes imprimir REQ de otra sede.")
        else:
            raise PermissionDenied("Rol no autorizado.")

    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/")

    tipo_req = getattr(req, "tipo_requerimiento", None) or TipoRequerimiento.ENTRE_SEDES

    if tipo_req == TipoRequerimiento.PROVEEDOR:
        template = "inventario/req_print_proveedor.html"
    else:
        template = "inventario/req_print_entre_sedes.html"

    total_cantidad = sum(int(it.cantidad or 0) for it in items)

    return render(request, template, {
        "req": req,
        "items": items,
        "total_cantidad": total_cantidad,
    })

@require_POST
@login_required
def req_set_tipo_doc(request, req_id: int):
    """
    Cambia tipo_requerimiento (y sede_destino) de un REQ ya creado (ej: PENDIENTE),
    desde dashboard almacén.
    - Solo CENTRAL (JAUJA) puede poner PROVEEDOR
    - ENTRE_SEDES requiere sede_destino CENTRAL
    """
    req = get_object_or_404(DocumentoInventario, id=req_id, tipo=TipoDocumento.REQ)

    try:
        profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
        sede_user = profile.get_sede_operativa()
        if not sede_user:
            raise ValidationError("No tienes sede operativa asignada.")
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
        return redirect("/dashboard/almacen/")

    tipo = (request.POST.get("tipo_requerimiento") or "").strip().upper()
    sede_destino_id = (request.POST.get("sede_destino_id") or "").strip()

    if tipo not in (TipoRequerimiento.PROVEEDOR, TipoRequerimiento.ENTRE_SEDES):
        messages.error(request, "Tipo de requerimiento inválido.")
        return redirect("/dashboard/almacen/")

    # ✅ PROVEEDOR solo CENTRAL (JAUJA)
    if tipo == TipoRequerimiento.PROVEEDOR:
        if sede_user.tipo != Sede.CENTRAL:
            messages.error(request, "Solo la sede CENTRAL (Jauja) puede generar requerimientos a PROVEEDOR.")
            return redirect("/dashboard/almacen/")

        req.tipo_requerimiento = TipoRequerimiento.PROVEEDOR
        req.sede_destino = None
        req.save(update_fields=["tipo_requerimiento", "sede_destino"])
        messages.success(request, f"REQ {req.numero or req.id}: tipo actualizado a PROVEEDOR.")
        return redirect("/dashboard/almacen/")

    # ✅ ENTRE_SEDES => requiere sede_destino CENTRAL
    if not sede_destino_id:
        messages.error(request, "Para ENTRE SEDES debes seleccionar una sede CENTRAL destino.")
        return redirect("/dashboard/almacen/")

    sede_destino = get_object_or_404(Sede, id=sede_destino_id, activo=True)
    if sede_destino.tipo != Sede.CENTRAL:
        messages.error(request, "El destino para ENTRE SEDES debe ser una sede CENTRAL.")
        return redirect("/dashboard/almacen/")

    req.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
    req.sede_destino = sede_destino
    req.save(update_fields=["tipo_requerimiento", "sede_destino"])

    messages.success(request, f"REQ {req.numero or req.id}: tipo actualizado a ENTRE SEDES.")
    return redirect("/dashboard/almacen/")
