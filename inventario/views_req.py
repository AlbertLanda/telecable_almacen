from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.core.exceptions import ValidationError, PermissionDenied
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import JsonResponse

from inventario.models import (
    Ubicacion,
    DocumentoInventario,
    UserProfile,
    TipoDocumento,
    EstadoDocumento,
    Stock,
    Producto,
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


def _serialize_cart(req: DocumentoInventario):
    items = []
    for it in req.items.select_related("producto").order_by("producto__nombre"):
        p = it.producto
        codigo = getattr(p, "codigo_interno", "") or getattr(p, "codigo", "") or ""
        items.append({
            "producto_id": p.id,
            "nombre": p.nombre,
            "codigo": codigo,
            "cantidad": int(it.cantidad or 0),
        })
    return items


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

    return render(
        request,
        "inventario/req_home.html",
        {
            "req": req,
            "ubicacion": ubicacion,
            "sede": sede,
            "items": req.items.select_related("producto").order_by("producto__nombre"),
        },
    )


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
        stocks = stocks.filter(producto__nombre__icontains=q) | stocks.filter(producto__codigo_interno__icontains=q)

    stocks = stocks[:80]

    data = []
    for s in stocks:
        p = s.producto
        data.append({
            "producto_id": p.id,
            "nombre": p.nombre,
            "codigo": getattr(p, "codigo_interno", "") or "",
            "disponible": int(s.cantidad or 0),
            "unidad": getattr(p, "unidad_medida", "") or "",
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
    return JsonResponse({"ok": True, "req_id": req.id, "items": _serialize_cart(req)})

@require_POST
@login_required
def req_set_qty(request):
    """
    AJAX: setear cantidad de un item del carrito (REQ BORRADOR)
    POST /req/set-qty/  (producto_id, cantidad)
    """
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
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
    is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    if not is_ajax:
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

    try:
        add_item_to_req(user=request.user, req=req, producto=producto, cantidad=cantidad)
    except (ValidationError, PermissionDenied) as e:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        messages.error(request, str(e))
        return redirect("/req/")

    # ✅ Si es AJAX: devolvemos carrito actualizado
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
