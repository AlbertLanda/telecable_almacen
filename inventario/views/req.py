from __future__ import annotations

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
    TipoRequerimiento,
    Proveedor
)

from inventario.services.req_service import (
    get_or_create_req_borrador,
    add_item_to_req,
    set_item_qty,
    remove_item_from_req,
)

from inventario.services.sal_service import req_to_sal
from inventario.services.lookup_service import buscar_producto_por_code
from inventario.services.req_service import clonar_req

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
        # Fallback: creamos una por defecto si no existe
        ubicacion = Ubicacion.objects.create(nombre="GENERAL", sede=sede)

    return ubicacion

def _get_sede_operativa(user):
    profile = getattr(user, "profile", None)
    if not profile:
        raise ValidationError("El usuario no tiene perfil (UserProfile).")
    sede = profile.get_sede_operativa()
    if not sede:
        raise ValidationError("No tienes sede operativa asignada.")
    return sede

def _get_sede_central():
    return Sede.objects.filter(tipo=Sede.CENTRAL, activo=True).order_by("nombre").first()

def _is_ajax(request) -> bool:
    return request.headers.get("X-Requested-With") == "XMLHttpRequest"

def _producto_codigo(p: Producto) -> str:
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

def _ensure_req_defaults(req: DocumentoInventario, user):
    """Normaliza REQ para que no choque con clean()."""
    changed = False
    profile = getattr(user, "profile", None)
    if not profile:
        return

    # Si viene vacío/null por data antigua, ponemos LOCAL
    if not getattr(req, "tipo_requerimiento", None):
        req.tipo_requerimiento = TipoRequerimiento.LOCAL
        changed = True

    # Técnico
    if profile.rol == UserProfile.Rol.SOLICITANTE:
        if req.tipo_requerimiento != TipoRequerimiento.LOCAL:
            req.tipo_requerimiento = TipoRequerimiento.LOCAL
            changed = True
        if req.sede_destino_id:
            req.sede_destino = None
            changed = True
        if getattr(req, "proveedor_id", None):
            req.proveedor = None
            changed = True

    # Almacén
    elif profile.rol == UserProfile.Rol.ALMACEN:
        sede_user = profile.get_sede_operativa()
        if sede_user and sede_user.tipo == Sede.CENTRAL:
            if req.tipo_requerimiento != TipoRequerimiento.PROVEEDOR:
                req.tipo_requerimiento = TipoRequerimiento.PROVEEDOR
                changed = True
            if req.sede_destino_id:
                req.sede_destino = None
                changed = True
        else:
            if req.tipo_requerimiento != TipoRequerimiento.ENTRE_SEDES:
                req.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
                changed = True
            central = _get_sede_central()
            if central and (not req.sede_destino_id or req.sede_destino_id != central.id):
                req.sede_destino = central
                changed = True
            if getattr(req, "proveedor_id", None):
                req.proveedor = None
                changed = True

    if changed:
        fields = ["tipo_requerimiento", "sede_destino"]
        if hasattr(req, "proveedor"):
            fields.append("proveedor")
        req.save(update_fields=fields)


# --------------------
# Vistas REQ (Técnico / General)
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
    _ensure_req_defaults(req, request.user)

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

@login_required
def req_home_almacen(request):
    """
    Vista específica para que el Almacenero cree REQ (a Proveedor o Entre Sedes)
    sin usar la interfaz del técnico.
    """
    try:
        profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
        sede = _get_sede_operativa(request.user)
        # Usamos una ubicación 'default' o administrativa
        ubicacion = _get_ubicacion_operativa(request.user) 
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
        return redirect("dash_almacen")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req, request.user)

    proveedores = Proveedor.objects.filter(activo=True).order_by("razon_social")
    
    return render(
        request,
        "inventario/req_home_almacen.html",
        {
            "req": req,
            "ubicacion": ubicacion,
            "sede": sede,
            "items": req.items.select_related("producto").order_by("producto__nombre"),
            "proveedores": proveedores,
        },
    )

@require_POST
@login_required
def req_set_tipo_requerimiento(request):
    try:
        profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA, UserProfile.Rol.ALMACEN)
        ubicacion = _get_ubicacion_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        if _is_ajax(request): return JsonResponse({"ok": False, "error": str(e)}, status=403)
        messages.error(request, str(e))
        return redirect("/req/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req, request.user)

    # Asegurar sede
    if req.tipo == TipoDocumento.REQ and not req.sede_id:
        sede_operativa = request.user.profile.get_sede_operativa()
        if sede_operativa:
            req.sede = sede_operativa
            req.save(update_fields=["sede"])

    tipo = (request.POST.get("tipo_requerimiento") or "").strip().upper()
    sede_destino_id = (request.POST.get("sede_destino_id") or "").strip()
    proveedor_id = (request.POST.get("proveedor_id") or "").strip()

    # SOLICITANTE: siempre LOCAL
    if profile.rol == UserProfile.Rol.SOLICITANTE:
        req.tipo_requerimiento = TipoRequerimiento.LOCAL
        req.sede_destino = None
        req.proveedor = None
        req.save(update_fields=["tipo_requerimiento", "sede_destino", "proveedor"])
        if _is_ajax(request): return JsonResponse({"ok": True})
        return redirect("/req/")

    # Lógica de cambio de tipo para Almacén/Jefa
    if tipo == TipoRequerimiento.PROVEEDOR:
        if req.sede and req.sede.tipo != Sede.CENTRAL:
            msg = "PROVEEDOR solo aplica si el REQ es de una sede CENTRAL."
            if _is_ajax(request): return JsonResponse({"ok": False, "error": msg}, status=400)
            return redirect("/req/")
        
        req.tipo_requerimiento = TipoRequerimiento.PROVEEDOR
        req.sede_destino = None
        if proveedor_id:
            req.proveedor = get_object_or_404(Proveedor, id=proveedor_id)
        req.save()

    elif tipo == TipoRequerimiento.ENTRE_SEDES:
        if req.sede and req.sede.tipo == Sede.CENTRAL:
            msg = "La sede CENTRAL no debe generar REQ 'ENTRE SEDES'."
            if _is_ajax(request): return JsonResponse({"ok": False, "error": msg}, status=400)
            return redirect("/req/")
        
        req.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
        req.proveedor = None
        if sede_destino_id:
            dest = get_object_or_404(Sede, id=sede_destino_id)
            if dest.tipo != Sede.CENTRAL:
                msg = "Destino debe ser CENTRAL."
                if _is_ajax(request): return JsonResponse({"ok": False, "error": msg}, status=400)
                return redirect("/req/")
            req.sede_destino = dest
        req.save()
    
    else: # LOCAL
        req.tipo_requerimiento = TipoRequerimiento.LOCAL
        req.sede_destino = None
        req.proveedor = None
        req.save()

    if _is_ajax(request): return JsonResponse({"ok": True})
    return redirect("/req/")

@login_required
def req_catalogo(request):
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA, UserProfile.Rol.ALMACEN)
        sede = _get_sede_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=403)

    q = (request.GET.get("q") or "").strip()
    modo = (request.GET.get("modo") or "").strip().lower()

    # Modo Proveedor: sin filtrar por stock, solo para Central
    if modo == "proveedor":
        if sede.tipo != Sede.CENTRAL:
            return JsonResponse({"ok": False, "error": "Solo CENTRAL usa modo proveedor."}, status=403)
        
        productos = Producto.objects.filter(activo=True).order_by("nombre")
        if q:
            productos = productos.filter(
                Q(nombre__icontains=q) | Q(codigo_interno__icontains=q) | Q(barcode__icontains=q)
            )
        productos = productos[:80]
        data = [{
            "producto_id": p.id,
            "nombre": p.nombre,
            "codigo": _producto_codigo(p),
            "disponible": None,
            "unidad": getattr(p, "unidad", "") or "",
        } for p in productos]
        return JsonResponse({"ok": True, "modo": "proveedor", "results": data})

    # Modo Local (default): filtra por stock > 0 en la sede
    stocks = Stock.objects.filter(sede=sede, producto__activo=True, cantidad__gt=0).select_related("producto").order_by("producto__nombre")
    if q:
        stocks = stocks.filter(
            Q(producto__nombre__icontains=q) |
            Q(producto__codigo_interno__icontains=q) |
            Q(producto__barcode__icontains=q)
        )
    stocks = stocks[:80]
    data = [{
        "producto_id": s.producto.id,
        "nombre": s.producto.nombre,
        "codigo": _producto_codigo(s.producto),
        "disponible": int(s.cantidad),
        "unidad": getattr(s.producto, "unidad", "") or "",
    } for s in stocks]
    return JsonResponse({"ok": True, "modo": "local", "results": data})

@login_required
def req_carrito(request):
    try:
        ubicacion = _get_ubicacion_operativa(request.user)
    except ValidationError:
        return JsonResponse({"ok": False, "error": "Sin ubicación operativa"}, status=403)

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    return JsonResponse({"ok": True, "req_id": req.id, "items": _serialize_cart(req)})

@require_POST
@login_required
def req_set_qty(request):
    if not _is_ajax(request): return JsonResponse({"ok": False}, status=400)
    try:
        ubicacion = _get_ubicacion_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    except Exception as e: return JsonResponse({"ok": False, "error": str(e)}, status=403)

    pid = request.POST.get("producto_id")
    try: qty = int(request.POST.get("cantidad"))
    except: return JsonResponse({"ok": False, "error": "Cant inválida"}, status=400)
    if qty <= 0: return JsonResponse({"ok": False, "error": "> 0"}, status=400)

    prod = get_object_or_404(Producto, id=pid)
    # Validar stock si no es JEFA/ADMIN o si es modo proveedor... (simplificado)
    # Por ahora permitimos setear qty y que el checkout valide
    item = set_item_qty(user=request.user, req=req, producto=prod, cantidad=qty)
    return JsonResponse({"ok": True, "cantidad": item.cantidad})

@require_POST
@login_required
def req_remove_producto(request):
    if not _is_ajax(request): return JsonResponse({"ok": False}, status=400)
    try:
        ubicacion = _get_ubicacion_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    except Exception: return JsonResponse({"ok": False}, status=403)

    pid = request.POST.get("producto_id")
    prod = get_object_or_404(Producto, id=pid)
    remove_item_from_req(user=request.user, req=req, producto=prod)
    return JsonResponse({"ok": True})

@require_POST
@login_required
def req_add_producto(request):
    try:
        ubicacion = _get_ubicacion_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    except Exception as e:
        if _is_ajax(request): return JsonResponse({"ok": False, "error": str(e)}, status=403)
        return redirect("/req/")

    pid = request.POST.get("producto_id")
    try: qty = int(request.POST.get("cantidad", 1))
    except: qty = 1
    if qty <= 0: qty = 1

    prod = get_object_or_404(Producto, id=pid)
    add_item_to_req(user=request.user, req=req, producto=prod, cantidad=qty)
    
    if _is_ajax(request):
        return JsonResponse({"ok": True, "message": "Agregado", "items": _serialize_cart(req)})
    return redirect("/req/")

@login_required
def req_add_item(request):
    """Versión simple para agregar por código (POST normal)"""
    if request.method != "POST": return redirect("/req/")
    code = request.POST.get("code", "").strip()
    try:
        ubicacion = _get_ubicacion_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
        prod = buscar_producto_por_code(code)
        if prod:
            add_item_to_req(user=request.user, req=req, producto=prod, cantidad=1)
            messages.success(request, f"Agregado: {prod.nombre}")
        else:
            messages.error(request, "Producto no encontrado")
    except Exception as e:
        messages.error(request, str(e))
    return redirect("/req/")

@require_POST
@login_required
def req_scan_add(request):
    """Para el módulo de escaneo"""
    code = request.POST.get("code", "").strip()
    ubi_id = request.POST.get("ubicacion_id")
    if not code or not ubi_id: return redirect("/req/")
    
    try:
        ubi = get_object_or_404(Ubicacion, id=ubi_id)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubi)
        prod = buscar_producto_por_code(code)
        if prod:
            add_item_to_req(user=request.user, req=req, producto=prod, cantidad=1)
            messages.success(request, f"Agregado: {prod.nombre}")
        else:
            messages.error(request, "Producto no encontrado")
    except Exception as e:
        messages.error(request, str(e))
    return redirect("/req/")

@require_POST
@login_required
def req_enviar(request, req_id: int):
    req = get_object_or_404(DocumentoInventario, id=req_id, tipo=TipoDocumento.REQ)
    if req.responsable_id != request.user.id:
        messages.error(request, "No es tu REQ.")
        return redirect("/req/")
    
    # Validaciones finales
    if req.tipo_requerimiento == TipoRequerimiento.PROVEEDOR and not req.proveedor:
        messages.error(request, "Falta proveedor.")
        return redirect("/req/")
    if req.tipo_requerimiento == TipoRequerimiento.ENTRE_SEDES and not req.sede_destino:
        messages.error(request, "Falta sede destino.")
        return redirect("/req/")

    try:
        req.enviar_req()
        messages.success(request, f"REQ enviado: {req.numero}")
    except ValidationError as e:
        messages.error(request, str(e))
    
    # Redirigir según quién lo envió
    if request.user.profile.rol == UserProfile.Rol.ALMACEN:
        return redirect("req_home_almacen")
    return redirect("/req/")

@require_POST
@login_required
def req_convert_to_sal(request, req_id: int):
    req = get_object_or_404(DocumentoInventario, id=req_id, tipo=TipoDocumento.REQ)
    try:
        sal = req_to_sal(user=request.user, req=req, responsable=request.user)
        messages.success(request, f"SAL creada: {sal.numero or sal.id}")
        return redirect(f"/sal/{sal.id}/")
    except Exception as e:
        messages.error(request, str(e))
        return redirect("/")

@login_required
def req_print(request, req_id: int):
    req = get_object_or_404(DocumentoInventario, id=req_id, tipo=TipoDocumento.REQ)
    items = req.items.select_related("producto").order_by("producto__nombre")
    
    template = "inventario/req_print_proveedor.html"
    if req.tipo_requerimiento == TipoRequerimiento.ENTRE_SEDES:
        template = "inventario/req_print_entre_sedes.html"
        
    return render(request, template, {
        "req": req,
        "items": items,
        "total_cantidad": sum(int(it.cantidad) for it in items)
    })

@require_POST
@login_required
def req_set_tipo_doc(request, req_id):
    """Acción rápida desde dashboard para cambiar tipo"""
    req = get_object_or_404(DocumentoInventario, id=req_id)
    tipo = request.POST.get("tipo_requerimiento")
    dest_id = request.POST.get("sede_destino_id")
    
    if tipo == "PROVEEDOR":
        if req.sede.tipo != Sede.CENTRAL:
            messages.error(request, "Solo Central")
            return redirect("dash_almacen")
        req.tipo_requerimiento = tipo
        req.save()
    elif tipo == "ENTRE_SEDES":
        if dest_id:
            req.sede_destino_id = dest_id
            req.tipo_requerimiento = tipo
            req.save()
    
    return redirect("dash_almacen")

@login_required
def req_clonar(request, req_id):
    """Botón para repetir un pedido anterior"""
    try:
        nuevo_req = clonar_req(request.user, req_id)
        messages.success(request, "Pedido duplicado correctamente. Revisa el carrito antes de enviar.")
        # Redirigir al home del REQ (que muestra el borrador actual)
        return redirect("req_home") 
    except Exception as e:
        messages.error(request, f"Error al clonar: {str(e)}")
        return redirect("tecnico_mis_reqs")
    
@login_required
def req_eliminar(request, req_id):
    """Permite eliminar un REQ solo si está en estado BORRADOR"""
    req = get_object_or_404(DocumentoInventario, id=req_id)
    
    # 1. Seguridad: Solo el dueño puede borrarlo
    if req.responsable != request.user:
        messages.error(request, "No tienes permiso para eliminar este requerimiento.")
        return redirect("tecnico_mis_reqs")
    
    # 2. Lógica: Solo borradores
    if req.estado != EstadoDocumento.REQ_BORRADOR:
        messages.error(request, "Solo se pueden eliminar borradores. Este pedido ya fue procesado.")
        return redirect("tecnico_mis_reqs")
    
    # 3. Eliminar
    req.delete()
    messages.success(request, "Borrador eliminado correctamente.")
    return redirect("tecnico_mis_reqs")

