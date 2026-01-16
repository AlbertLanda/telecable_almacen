from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.core.exceptions import ValidationError, PermissionDenied
from django.contrib import messages
from django.views.decorators.http import require_POST

from inventario.models import Ubicacion, DocumentoInventario, UserProfile, TipoDocumento, EstadoDocumento
from inventario.services.req_service import get_or_create_req_borrador, add_item_to_req
from inventario.services.sal_service import req_to_sal
from inventario.services.scan_service import buscar_producto_y_stock
from inventario.services.lookup_service import buscar_producto_por_code


def _require_roles(user, *roles):
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")
    return profile


def _get_ubicacion_operativa(user):
    """
    Define la ubicación operativa por defecto del usuario:
    - Usa la sede operativa del profile.
    - Toma la primera ubicación de esa sede (por ahora RACK-1A suele ser la 1).
    """
    profile = getattr(user, "profile", None)
    if not profile:
        raise ValidationError("El usuario no tiene perfil (UserProfile).")

    sede = profile.get_sede_operativa()
    if not sede:
        raise ValidationError("No tienes sede operativa asignada.")

    ubicacion = Ubicacion.objects.filter(sede=sede).order_by("nombre").first()
    if not ubicacion:
        raise ValidationError(f"No hay ubicaciones creadas para la sede {sede.nombre}.")

    return ubicacion


@login_required
def req_home(request):
    """
    Vista principal REQ estilo 'supermercado' (solo SOLICITANTE/JEFA):
    - muestra el REQ borrador actual
    - input para escanear (q)
    - lista de ítems
    - botón enviar REQ
    """
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        ubicacion = _get_ubicacion_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
        return redirect("/admin/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)

    # Si viene un scan por GET ?q=...
    code = (request.GET.get("q") or "").strip()
    if code:
        producto, _, error = buscar_producto_y_stock(code)
        if error:
            messages.error(request, error)
        elif producto:
            try:
                add_item_to_req(user=request.user, req=req, producto=producto, cantidad=1)
                messages.success(request, f"Agregado: {producto.nombre}")
                return redirect("/req/")  # evita re-agregar por refresh
            except ValidationError as e:
                messages.error(request, str(e))

    return render(
        request,
        "inventario/req_home.html",
        {
            "req": req,
            "ubicacion": ubicacion,
            "items": req.items.select_related("producto").order_by("producto__nombre"),
        },
    )


@login_required
def req_add_item(request):
    """
    Agregar por POST usando código escaneado (barcode / codigo_interno / serial).
    Solo SOLICITANTE/JEFA.
    """
    if request.method != "POST":
        return redirect("/req/")

    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/req/")

    code = (request.POST.get("code") or "").strip()
    if not code:
        messages.error(request, "Escanea un código válido.")
        return redirect("/req/")

    try:
        ubicacion = _get_ubicacion_operativa(request.user)
    except ValidationError as e:
        messages.error(request, str(e))
        return redirect("/req/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)

    producto, _, error = buscar_producto_y_stock(code)
    if error:
        messages.error(request, error)
        return redirect("/req/")

    if not producto:
        messages.error(request, "Producto no encontrado.")
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
    """
    Agrega al REQ usando código escaneado (barcode o codigo_interno).
    Solo SOLICITANTE/JEFA.
    """
    try:
        profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/req/")

    code = (request.POST.get("code") or "").strip()
    ubicacion_id = request.POST.get("ubicacion_id")

    if not ubicacion_id:
        return redirect("/req/")

    ubicacion = get_object_or_404(Ubicacion, id=ubicacion_id)

    # ✅ seguridad por sede: no permitir ubicaciones de otra sede (excepto JEFA)
    sede_user = profile.get_sede_operativa()
    if profile.rol != UserProfile.Rol.JEFA and ubicacion.sede_id != sede_user.id:
        messages.error(request, "No puedes usar una ubicación de otra sede.")
        return redirect("/req/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)

    if not code:
        return render(
            request,
            "inventario/req_home.html",
            {
                "req": req,
                "ubicacion": ubicacion,
                "items": req.items.select_related("producto").order_by("producto__nombre"),
                "error": "Escanea o escribe un código.",
            },
        )

    producto = buscar_producto_por_code(code)
    if not producto:
        return render(
            request,
            "inventario/req_home.html",
            {
                "req": req,
                "ubicacion": ubicacion,
                "items": req.items.select_related("producto").order_by("producto__nombre"),
                "error": f"No se encontró producto con código: {code}",
            },
        )

    try:
        add_item_to_req(user=request.user, req=req, producto=producto, cantidad=1)
        messages.success(request, f"Agregado: {producto.nombre}")
    except ValidationError as e:
        messages.error(request, str(e))

    return redirect(f"/req/?ubicacion_id={ubicacion.id}")


@require_POST
@login_required
def req_enviar(request, req_id: int):
    """
    Enviar REQ (SOLICITANTE/JEFA):
    pasa de REQ_BORRADOR -> REQ_PENDIENTE y asigna número.
    """
    req = get_object_or_404(DocumentoInventario, id=req_id)

    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/req/")

    if req.responsable_id != request.user.id:
        messages.error(request, "No puedes enviar un REQ que no es tuyo.")
        return redirect("/req/")

    if req.tipo != TipoDocumento.REQ:
        messages.error(request, "Este documento no es un REQ.")
        return redirect("/req/")

    try:
        req.enviar_req()
        messages.success(request, f"REQ enviado: {req.numero}")
    except ValidationError as e:
        messages.error(request, str(e))

    return redirect("/req/")


@login_required
def req_convert_to_sal(request, req_id: int):
    """
    Generar SAL desde REQ (ALMACÉN/JEFA):
    - Solo desde REQ_PENDIENTE
    - Respeta sede: almacén solo atiende REQ de su sede (JEFA puede todo)
    """
    if request.method != "POST":
        return redirect("/req/")

    req = get_object_or_404(DocumentoInventario, id=req_id)

    try:
        profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/req/")

    if req.tipo != TipoDocumento.REQ:
        messages.error(request, "Este documento no es un REQ.")
        return redirect("/req/")

    if req.estado != EstadoDocumento.REQ_PENDIENTE:
        messages.error(request, "Solo puedes generar SAL desde un REQ PENDIENTE.")
        return redirect("/req/")

    sede_user = profile.get_sede_operativa()
    if profile.rol != UserProfile.Rol.JEFA and req.sede_id != sede_user.id:
        messages.error(request, "No puedes atender REQ de otra sede.")
        return redirect("/req/")

    try:
        sal = req_to_sal(user=request.user, req=req, responsable=request.user)
        messages.success(request, f"SAL creada (borrador): {sal.numero or 'sin número aún'}.")
        return redirect(f"/sal/{sal.id}/")
    except ValidationError as e:
        messages.error(request, str(e))
        return redirect("/req/")
