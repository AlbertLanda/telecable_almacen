from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.core.exceptions import ValidationError, PermissionDenied
from django.contrib import messages
from django.views.decorators.http import require_POST

from inventario.models import (
    Ubicacion,
    DocumentoInventario,
    UserProfile,
    TipoDocumento,
    EstadoDocumento,
)

from inventario.services.req_service import get_or_create_req_borrador, add_item_to_req
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
    """
    Obtiene la ubicación operativa por defecto del usuario:
    - Usa la sede operativa del profile
    - Toma la primera ubicación de esa sede
    """
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


# --------------------
# Vistas REQ
# --------------------
@login_required
def req_home(request):
    """
    Vista principal del REQ (tipo carrito).
    Solo SOLICITANTE / JEFA
    """
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        ubicacion = _get_ubicacion_operativa(request.user)
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
            "items": req.items.select_related("producto").order_by("producto__nombre"),
        },
    )


@login_required
def req_add_item(request):
    """
    Agregar ítem al REQ por POST usando 'code' (sin necesidad de ubicacion_id).
    Solo SOLICITANTE / JEFA.
    (Esta vista existe porque tu urls.py la está importando.)
    """
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
    """
    Agregar ítem al REQ mediante código escaneado (requiere ubicacion_id).
    Solo SOLICITANTE / JEFA
    """
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

    # Seguridad por sede (JEFA puede todo)
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
    """
    Enviar REQ:
    BORRADOR -> PENDIENTE
    Solo SOLICITANTE / JEFA
    """
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
    """
    Convertir REQ -> SAL
    SOLO ALMACÉN / JEFA
    """
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
