from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.contrib.auth import get_user_model
from django.shortcuts import render, redirect, get_object_or_404
from django.core.exceptions import ValidationError, PermissionDenied
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.db.models import Q
from django.utils import timezone
from inventario.models import Stock

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
    Proveedor,
    ItemSerializado,
    DocumentoItem,
    DocumentoItemSerializado,
)

from inventario.services.req_service import (
    get_or_create_req_borrador,
    add_item_to_req,
    set_item_qty,
    remove_item_from_req
)

from inventario.services.sal_service import req_to_sal
from inventario.services.lookup_service import buscar_producto_por_code
from inventario.services.req_service import clonar_req
from inventario.permissions import role_required, sede_required, get_profile
from django.db import transaction
from inventario.models import StockTecnico, DocumentoItem, MovimientoInventario
import json
from django.db import IntegrityError

User = get_user_model()
# --------------------
# Helpers
# --------------------
def _require_roles(user, *roles):
    profile = get_profile(user)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")
    return profile

def _get_ubicacion_operativa(user):
    profile = get_profile(user)
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
        ubicacion = Ubicacion.objects.create(nombre="GENERAL", sede=sede)

    return ubicacion


def _get_sede_operativa(user):
    profile = get_profile(user)
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

def normalizar_codigo_barras(valor: str) -> str:
    """
    Normaliza códigos escaneados por pistola.
    Algunos lectores envían el guion '-' como comilla simple "'"
    por configuración de teclado.
    """
    if not valor:
        return ""

    return (
        valor.strip()
        .upper()
        .replace("'", "-")
        .replace("´", "-")
        .replace("`", "-")
        .replace(" ", "")
    )

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
    """
    Vista principal inteligente.
    """
    profile = getattr(request.user, 'profile', None)
    if not profile:
        return redirect('logout')
    
    # 1. Obtener Ubicación Segura
    ubicacion_obj = Ubicacion.objects.filter(sede=profile.get_sede_operativa()).order_by('nombre').first()
    
    # 2. BUSCAR BORRADOR EXISTENTE
    req_borrador = DocumentoInventario.objects.filter(
        responsable=request.user,
        estado__in=[EstadoDocumento.BORRADOR, EstadoDocumento.REQ_BORRADOR], 
        tipo=TipoDocumento.REQ
    ).first()

    # Solo si NO existe ninguno, dejamos que el servicio cree uno nuevo
    if not req_borrador:
        req_borrador = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion_obj)

    # 🟢 FIX 1: Forzar que el tipo sea LOCAL si es técnico, para que no salga "Entre Sedes"
    _ensure_req_defaults(req_borrador, request.user)

    # 3. Configurar Entorno (Sede y Stock)
    sede_usuario = profile.get_sede_operativa()
    sede_consulta = sede_usuario 
    nombre_stock_visible = "Mi Stock"

    # Si hay un borrador ENTRE_SEDES, cambiamos la consulta a la CENTRAL
    if req_borrador.tipo_requerimiento == TipoRequerimiento.ENTRE_SEDES:
        central = Sede.objects.filter(tipo=Sede.CENTRAL, activo=True).first()
        if central:
            sede_consulta = central
            nombre_stock_visible = f"Stock {central.nombre}"

    # 4. Lógica de Búsqueda
    query = request.GET.get('q', '').strip()
    productos = []
    
    if query:
        productos = Producto.objects.filter(
            Q(nombre__icontains=query) | 
            Q(codigo__icontains=query) | 
            Q(codigo_interno__icontains=query),
            activo=True
        )[:20]

        for p in productos:
            stock_obj = Stock.objects.filter(producto=p, sede=sede_consulta).first()
            p.stock_visible = stock_obj.cantidad if stock_obj else 0
            p.sede_visible_nombre = nombre_stock_visible

    # 5. Contexto
    ctx = {
        'req': req_borrador,
        'productos': productos,
        'busqueda': query,
        'nombre_stock_visible': nombre_stock_visible,
        'sede': sede_usuario,
        'ubicacion': ubicacion_obj,
        'proveedores': Proveedor.objects.filter(activo=True).order_by('razon_social'),
        'sedes_central': Sede.objects.filter(tipo=Sede.CENTRAL, activo=True)
    }

    # Selección de Template
    if profile.rol in [UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN]:
        return render(request, 'inventario/req_home_almacen.html', ctx)

    return render(request, 'inventario/req_home.html', ctx)


@login_required
def req_home_almacen(request):
    """
    Vista específica para que el Almacenero cree REQ.
    """
    try:
        profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
        sede = _get_sede_operativa(request.user)
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
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA, UserProfile.Rol.ALMACEN, UserProfile.Rol.ADMIN)
        sede_usuario = _get_sede_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=403)

    q = (request.GET.get("q") or "").strip()
    modo = (request.GET.get("modo") or "").strip().lower()

    if modo == "proveedor":
        if sede_usuario.tipo != Sede.CENTRAL:
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

    sede_consulta = sede_usuario 

    if modo == "entre_sedes":
        central = Sede.objects.filter(tipo=Sede.CENTRAL, activo=True).first()
        if central:
            sede_consulta = central
    
    stocks = Stock.objects.filter(
        sede=sede_consulta, 
        producto__activo=True, 
        cantidad__gt=0
    ).select_related("producto").order_by("producto__nombre")

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

    return JsonResponse({"ok": True, "modo": modo or "local", "results": data})


@login_required
def req_carrito(request):
    try:
        ubicacion = _get_ubicacion_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
        return JsonResponse({"ok": True, "req_id": req.id, "items": _serialize_cart(req)})
    except Exception as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=403)

@require_POST
@login_required
def req_set_qty(request):
    if not _is_ajax(request): return JsonResponse({"ok": False}, status=400)
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
        ubicacion = _get_ubicacion_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    except Exception as e: return JsonResponse({"ok": False, "error": str(e)}, status=403)

    pid = request.POST.get("producto_id")
    try: qty = int(request.POST.get("cantidad"))
    except: return JsonResponse({"ok": False, "error": "Cant inválida"}, status=400)
    if qty <= 0: return JsonResponse({"ok": False, "error": "> 0"}, status=400)

    prod = get_object_or_404(Producto, id=pid)
    item = set_item_qty(user=request.user, req=req, producto=prod, cantidad=qty)
    return JsonResponse({"ok": True, "cantidad": item.cantidad})

@require_POST
@login_required
def req_remove_producto(request):
    if not _is_ajax(request): return JsonResponse({"ok": False}, status=400)
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
        ubicacion = _get_ubicacion_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    except Exception as e: 
        return JsonResponse({"ok": False, "error": str(e)}, status=403)

    pid = request.POST.get("producto_id")
    prod = get_object_or_404(Producto, id=pid)
    remove_item_from_req(user=request.user, req=req, producto=prod)
    return JsonResponse({"ok": True})

@require_POST
@login_required
def req_add_producto(request):
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
        
        ubicacion = _get_ubicacion_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)

        pid = request.POST.get("producto_id")
        try: qty = int(request.POST.get("cantidad", 1))
        except: qty = 1
        if qty <= 0: qty = 1

        prod = get_object_or_404(Producto, id=pid)
        add_item_to_req(user=request.user, req=req, producto=prod, cantidad=qty)
        
        if _is_ajax(request):
            return JsonResponse({"ok": True, "message": "Agregado", "items": _serialize_cart(req)})
        return redirect("/req/")

    except Exception as e:
        if _is_ajax(request): return JsonResponse({"ok": False, "error": str(e)}, status=403)
        return redirect("/req/")


@login_required
def req_add_item(request):
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
    
    # 1. Seguridad
    if req.responsable_id != request.user.id:
        messages.error(request, "No puedes enviar un REQ que no es tuyo.")
        return redirect("req_home")
    
    # 2. Validación de Estado
    if req.estado not in [EstadoDocumento.BORRADOR, EstadoDocumento.REQ_BORRADOR]:
        messages.error(request, "El requerimiento ya fue enviado o procesado.")
        return redirect("req_home")

    # 3. Validar contenido
    if not req.items.exists():
        messages.error(request, "El carrito está vacío.")
        return redirect("req_home")

    # 4. Validar Configuración
    if req.tipo_requerimiento == TipoRequerimiento.PROVEEDOR:
        if not req.proveedor_manual: 
            messages.error(request, "⚠️ Falta escribir y GUARDAR el nombre del PROVEEDOR en el Paso 1.")
            return redirect("req_home") 
    elif req.tipo_requerimiento == TipoRequerimiento.ENTRE_SEDES:
        if not req.sede_destino:
            messages.error(request, "⚠️ Falta seleccionar la SEDE DESTINO en el Paso 1.")
            return redirect("req_home")

    # 5. Procesar
    try:
        # 🟢 FIX 2: Generar el número ANTES de guardar
        req.asignar_numero_si_falta()
        
        req.estado = EstadoDocumento.REQ_PENDIENTE
        req.fecha = timezone.now()
        req.save()
        
        # Mensajes
        if req.tipo_requerimiento == TipoRequerimiento.PROVEEDOR:
            messages.success(request, f"✅ Orden de Compra {req.numero} generada exitosamente.")
        else:
            messages.success(request, f"✅ Requerimiento {req.numero} enviado a Almacén.")

        # Redirección
        profile = getattr(request.user, 'profile', None)
        if profile and profile.rol in [UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN]:
            return redirect("dash_almacen")
        
        return redirect("tecnico_mis_reqs")

    except Exception as e:
        messages.error(request, f"Error al procesar: {str(e)}")
        return redirect("req_home")
    

@login_required
def req_convert_to_sal(request, req_id: int):
    # Esta función queda obsoleta para despacho manual, 
    # pero la dejamos por si tienes enlaces viejos.
    # Ahora usamos req_atender.
    return redirect("req_atender", req_id=req_id)

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


@login_required
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
def req_recepcionar_compra(request, req_id: int):
    """
    Convierte un REQ de PROVEEDOR en un INGRESO (ING).
    """
    req = get_object_or_404(DocumentoInventario, id=req_id, tipo=TipoDocumento.REQ)
    
    if req.tipo_requerimiento != TipoRequerimiento.PROVEEDOR:
        messages.error(request, "Este no es un pedido a proveedor.")
        return redirect("dash_almacen")
    
    profile = get_profile(request.user)
    if profile and profile.rol == UserProfile.Rol.ALMACEN:
        sede_user = profile.get_sede_operativa()
        if sede_user and req.sede_id != sede_user.id:
            raise PermissionDenied("No puedes atender REQ de otra sede.")

    if request.method == "POST":
        try:
            with transaction.atomic():
                # 0. Ubicación por defecto
                ubicacion_defecto = Ubicacion.objects.filter(sede=req.sede).first()
                if not ubicacion_defecto:
                    ubicacion_defecto = Ubicacion.objects.create(
                        sede=req.sede, nombre="RECEPCION", descripcion="Ubicación automática de ingreso"
                    )

                # 1. Crear el Ingreso (ING)
                ing = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.ING,
                    estado=EstadoDocumento.CONFIRMADO,
                    sede=req.sede,             
                    responsable=request.user,  
                    proveedor=req.proveedor,   
                    origen=req,                
                    referencia=f"Llegada Compra {req.numero}",
                    fecha=timezone.now(),
                    observaciones=request.POST.get('notas', '')
                )
                ing.asignar_numero_si_falta()

                items_req = req.items.all()
                
                # 2. Procesar cada ítem del pedido
                for item in items_req:
                    qty_llegada = int(request.POST.get(f'qty_{item.id}', 0))
                    
                    if qty_llegada > 0:
                        # A. Registrar Item
                        DocumentoItem.objects.create(
                            documento=ing,
                            producto=item.producto,
                            cantidad=qty_llegada,
                            observacion=item.observacion
                        )

                        # B. Movimiento Físico
                        mov = MovimientoInventario.objects.create(
                            producto=item.producto,
                            sede=req.sede,
                            tipo=MovimientoInventario.TIPO_IN,
                            qty=qty_llegada,
                            referencia=ing.numero,
                            usuario=request.user,
                            nota=f"Compra {req.numero}"
                        )
                        mov.aplicar() 

                        # C. SERIALIZADOS
                        if item.producto.es_serializado:
                            # --- RANGO ---
                            rango_inicio = request.POST.get(f'rango_inicio_{item.id}')
                            rango_fin = request.POST.get(f'rango_fin_{item.id}')
                            
                            if rango_inicio and rango_fin:
                                try:
                                    start = int(rango_inicio)
                                    end = int(rango_fin)
                                    items_a_crear = []
                                    for num in range(start, end + 1):
                                        serial_str = str(num)
                                        items_a_crear.append(ItemSerializado(
                                            producto=item.producto,
                                            serial=serial_str,
                                            ubicacion=ubicacion_defecto,
                                            estado=ItemSerializado.Estado.EN_ALMACEN
                                        ))
                                    ItemSerializado.objects.bulk_create(items_a_crear, ignore_conflicts=True)
                                except ValueError:
                                    pass

                            # --- DETALLE ONUs ---
                            json_data = request.POST.get(f'detalles_json_{item.id}')
                            if json_data:
                                try:
                                    lista_series = json.loads(json_data)
                                    for data in lista_series:
                                        sn = data.get('sn', '').strip().upper()
                                        mac = data.get('mac', '').strip().upper()
                                        cod44 = data.get('cod44', '').strip().upper()
                                        
                                        if sn: 
                                            try:
                                                ItemSerializado.objects.create(
                                                    producto=item.producto,
                                                    serial=sn,
                                                    mac_address=mac,
                                                    codigo_trazabilidad=cod44,
                                                    ubicacion=ubicacion_defecto,
                                                    estado=ItemSerializado.Estado.EN_ALMACEN
                                                )
                                            except IntegrityError:
                                                pass
                                except json.JSONDecodeError:
                                    pass

                # 3. Cerrar el REQ
                req.estado = EstadoDocumento.REQ_ATENDIDO
                req.save()

                messages.success(request, f"✅ Compra ingresada correctamente. Activos serializados registrados.")
                return redirect("dash_almacen")

        except Exception as e:
            messages.error(request, f"Error al procesar: {str(e)}")
            return redirect("dash_almacen")

    return render(request, 'inventario/req_recepcionar_form.html', {'req': req})


@require_POST
@login_required
def req_set_tipo(request):
    """
    Guarda la configuración del REQ (Paso 1).
    """
    try:
        req_id = request.POST.get("req_id")
        req_borrador = None
        
        if req_id:
            req_borrador = DocumentoInventario.objects.filter(id=req_id, responsable=request.user).first()
        
        if not req_borrador:
            req_borrador = DocumentoInventario.objects.filter(
                responsable=request.user,
                estado__in=[EstadoDocumento.BORRADOR, EstadoDocumento.REQ_BORRADOR],
                tipo=TipoDocumento.REQ
            ).first()

        if not req_borrador:
            return JsonResponse({"ok": False, "error": "No se encontró ningún borrador activo para guardar."})

        tipo = request.POST.get("tipo_requerimiento")
        
        if tipo == "PROVEEDOR":
            nombre_prov = request.POST.get("proveedor_manual", "").strip()
            if not nombre_prov:
                return JsonResponse({"ok": False, "error": "El nombre del proveedor no puede estar vacío."})
            
            req_borrador.tipo_requerimiento = TipoRequerimiento.PROVEEDOR
            req_borrador.proveedor_manual = nombre_prov 
            req_borrador.proveedor = None 
            req_borrador.sede_destino = None
            req_borrador.save()

        elif tipo == "ENTRE_SEDES":
            destino_id = request.POST.get("sede_destino_id")
            if not destino_id:
                return JsonResponse({"ok": False, "error": "Falta seleccionar la sede destino."})
            
            req_borrador.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
            req_borrador.sede_destino_id = destino_id
            req_borrador.proveedor_manual = None
            req_borrador.save()

        return JsonResponse({"ok": True})

    except Exception as e:
        return JsonResponse({"ok": False, "error": f"Error interno: {str(e)}"})
    

@login_required
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
def req_atender(request, req_id: int):
    """
    Despacha un REQ solicitado por un TÉCNICO o por OTRA SEDE.
    Mueve stock de Almacén -> Mochila (Técnico) o Transfiere (Sedes).
    """
    req = get_object_or_404(DocumentoInventario, id=req_id, tipo=TipoDocumento.REQ)
    
    # Validaciones básicas
    if req.estado != EstadoDocumento.REQ_PENDIENTE:
        messages.error(request, "Este requerimiento no está pendiente.")
        return redirect("dash_almacen")

    # 🔥 CORRECCIÓN CLAVE: Determinar quién despacha (quién pierde el stock)
    if req.tipo_requerimiento == TipoRequerimiento.ENTRE_SEDES and req.sede_destino:
        # Si Oroya pide a Jauja, Jauja (sede_destino) es quien despacha
        sede_despachador = req.sede_destino
    else:
        # Si es pedido Local, la misma sede del REQ despacha
        sede_despachador = req.sede

    # Validación de Permisos (¿Soy yo quien debe despachar?)
    profile = get_profile(request.user)
    if profile and profile.rol == UserProfile.Rol.ALMACEN:
        sede_user = profile.get_sede_operativa()
        if sede_user and sede_user.id != sede_despachador.id:
             raise PermissionDenied(f"No puedes despachar mercadería de {sede_despachador.nombre}.")
    
    # --- PROCESO POST (Guardar Despacho) ---
    if request.method == "POST":
        try:
            with transaction.atomic():
                
                if not req.numero:
                    req.asignar_numero_si_falta()

                # 1. Crear el documento de SALIDA (SAL)
                sal = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.SAL,
                    estado=EstadoDocumento.CONFIRMADO,
                    sede=sede_despachador,      # <--- AQUÍ SE CORRIGIÓ (Sale de Jauja)
                    sede_destino=req.sede,      # <--- Destino es Oroya (quien pidió)
                    responsable=request.user,
                    solicitante=req.responsable,
                    origen=req,
                    referencia=f"Atención de {req.numero}",
                    fecha=timezone.now(),
                    observaciones=request.POST.get('notas', '')
                )
                sal.asignar_numero_si_falta()

                items_req = req.items.all()
                total_items_procesados = 0  # Inicializamos contador

                for item in items_req:
                    qty_despacho = int(request.POST.get(f'qty_{item.id}', 0))

                    if item.producto.es_serializado and qty_despacho != int(item.cantidad):
                        raise ValueError(
                            f"Para {item.producto.nombre}, debes despachar exactamente "
                            f"{int(item.cantidad)} equipo(s) serializado(s). Seleccionados: {qty_despacho}."
                        )

                    if qty_despacho > 0:
                        total_items_procesados += 1

                        # A. Documento Item
                        DocumentoItem.objects.create(
                            documento=sal,
                            producto=item.producto,
                            cantidad=qty_despacho,
                            observacion=item.observacion
                        )

                        # B. Movimiento Físico (Resta stock al despachador)
                        mov = MovimientoInventario.objects.create(
                            producto=item.producto,
                            sede=sede_despachador,  # <--- AQUÍ SE CORRIGIÓ (Resta a Jauja)
                            tipo=MovimientoInventario.TIPO_OUT,
                            qty=qty_despacho,
                            referencia=sal.numero,
                            usuario=request.user,
                            nota=f"Despacho a {req.sede.nombre}"
                        )
                        mov.aplicar() # ¡Aquí baja el stock!

                        # C. Mochila Técnica (SOLO SI ES LOCAL)
                        if req.tipo_requerimiento == TipoRequerimiento.LOCAL:
                            stock_tech, _ = StockTecnico.objects.get_or_create(
                                tecnico=req.responsable,
                                producto=item.producto
                            )
                            stock_tech.cantidad += qty_despacho
                            stock_tech.save()

                        # D. SERIALIZADOS
                        if item.producto.es_serializado:
                            # ... (Lógica de rangos y ONUs igual que antes, 
                            # PERO buscando en sede_despachador)
                            
                            rango_inicio = request.POST.get(f'rango_inicio_{item.id}')
                            rango_fin = request.POST.get(f'rango_fin_{item.id}')
                            
                            if rango_inicio and rango_fin:
                                try:
                                    start = int(rango_inicio)
                                    end = int(rango_fin)
                                    items_to_update = ItemSerializado.objects.filter(
                                        producto=item.producto,
                                        ubicacion__sede=sede_despachador, # <--- Busca en Jauja
                                        estado=ItemSerializado.Estado.EN_ALMACEN,
                                        serial__gte=str(start), 
                                        serial__lte=str(end)
                                    )
                                    items_to_update.update(
                                        estado=ItemSerializado.Estado.ASIGNADO, # O EN_TRANSITO si prefieres
                                        asignado_a=req.responsable, 
                                        ubicacion=None 
                                    )
                                except ValueError: pass

                            json_ids = request.POST.get(f'detalles_json_{item.id}')
                            if json_ids:
                                try:
                                    ids_seleccionados = json.loads(json_ids)
                                    items_onu = ItemSerializado.objects.filter(
                                        id__in=ids_seleccionados,
                                        producto=item.producto,
                                        estado=ItemSerializado.Estado.EN_ALMACEN
                                    )
                                    items_onu.update(
                                        estado=ItemSerializado.Estado.ASIGNADO,
                                        asignado_a=req.responsable,
                                        ubicacion=None
                                    )
                                except: pass
                
                # Validación de seguridad
                if total_items_procesados == 0:
                    raise ValueError("⚠️ No has seleccionado ninguna cantidad para despachar.")
                            
                # Cerrar REQ
                req.estado = EstadoDocumento.REQ_ATENDIDO
                req.save()

                messages.success(request, f"✅ Despacho realizado. Stock descontado de {sede_despachador.nombre}.")
                return redirect("dash_almacen")

        except Exception as e:
            messages.error(request, f"Error al despachar: {str(e)}")
            return redirect("dash_almacen")

    # --- GET: Preparar datos para el Template ---
    items_context = []
    
    # 🔥 CORRECCIÓN VISUAL: Mostrar stock del despachador (Jauja), no del solicitante (Oroya)
    if req.tipo_requerimiento == TipoRequerimiento.ENTRE_SEDES and req.sede_destino:
        sede_visualizar = req.sede_destino
    else:
        sede_visualizar = req.sede

    for item in req.items.all():
        stock_obj = Stock.objects.filter(producto=item.producto, sede=sede_visualizar).first()
        stock_total = stock_obj.cantidad if stock_obj else 0

        disponibles = []
        if item.producto.es_serializado:
            qs = ItemSerializado.objects.filter(
                producto=item.producto,
                ubicacion__sede=sede_visualizar,
                estado=ItemSerializado.Estado.EN_ALMACEN
            ).only('id', 'serial', 'codigo_trazabilidad', 'mac_address', 'serial_secundario')[:500] 
            
            disponibles = [{'id': x.id, 'serial': x.serial, 'cod44': x.codigo_trazabilidad or '', 'mac': x.mac_address or '', 'serial_secundario': x.serial_secundario or ''} for x in qs]
        
        seriales_preseleccionados = []

        if item.producto.es_serializado:
            seriales_preseleccionados = [
                {
                    "id": s.item_serializado.id,
                    "serial": s.item_serializado.serial,
                    "cod44": s.item_serializado.codigo_trazabilidad or "",
                    "mac": s.item_serializado.mac_address or "",
                    "serial_secundario": s.item_serializado.serial_secundario or "",
                }
                for s in item.seriales_seleccionados.select_related("item_serializado").all()
            ]

        seriales_preseleccionados = []
        seriales_preseleccionados_ids = []

        if item.producto.es_serializado:
            seriales_qs = item.seriales_seleccionados.select_related("item_serializado").all()

            seriales_preseleccionados = [
                {
                    "id": s.item_serializado.id,
                    "serial": s.item_serializado.serial or "",
                    "cod44": s.item_serializado.codigo_trazabilidad or "",
                    "mac": s.item_serializado.mac_address or "",
                    "serial_secundario": s.item_serializado.serial_secundario or "",
                }
                for s in seriales_qs
            ]

            seriales_preseleccionados_ids = [
                s["id"] for s in seriales_preseleccionados
            ]

        items_context.append({
            'req_item': item,
            'disponibles_json': json.dumps(disponibles),
            'seriales_preseleccionados_json': json.dumps(seriales_preseleccionados),
            'stock_total': stock_total,
            "seriales_preseleccionados": seriales_preseleccionados,
            "seriales_preseleccionados_json": json.dumps(seriales_preseleccionados),
            "seriales_preseleccionados_ids_json": json.dumps(seriales_preseleccionados_ids),
        })

    context = {'req': req, 'items_context': items_context}
    return render(request, 'inventario/req_despachar_form.html', context)

@login_required
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
def req_asignacion_directa(request):
    """
    Vista para que Almacén cargue material directamente a un técnico (Push).
    Crea un REQ y lo prepara para despacho inmediato.
    """
    profile = get_profile(request.user)
    sede = profile.get_sede_operativa()
    ubicacion = _get_ubicacion_operativa(request.user)

    if request.method == 'POST':
        tecnico_id = request.POST.get('tecnico_id')
        req_id = request.POST.get('req_id')
        
        if not tecnico_id or not req_id:
            messages.error(request, "Falta seleccionar técnico o pedido.")
            return redirect('req_asignacion_directa')

        tecnico = get_object_or_404(User, id=tecnico_id)
        req = get_object_or_404(DocumentoInventario, id=req_id)
        
        # Validar que no esté vacía
        if not req.items.exists():
            messages.error(request, "La mochila está vacía. Agrega productos antes de confirmar.")
            return redirect('req_asignacion_directa')

        # Asignamos al técnico como el "Solicitante" real
        req.responsable = tecnico 
        req.solicitante = tecnico
        req.tipo_requerimiento = TipoRequerimiento.LOCAL
        
        # 🚀 CAMBIO CLAVE: CAMBIAR ESTADO A PENDIENTE Y GENERAR NUMERO
        req.estado = EstadoDocumento.REQ_PENDIENTE 
        req.asignar_numero_si_falta() # Le pone el código REQ-000XXX
        
        req.save()
        
        # Lo mandamos directo a ATENDER (Despacho)
        return redirect('req_atender', req_id=req.id)

    # ... (El resto de la función GET se queda igual) ...
    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    
    if req.tipo_requerimiento != TipoRequerimiento.LOCAL:
        req.tipo_requerimiento = TipoRequerimiento.LOCAL
        req.sede_destino = None
        req.proveedor = None
        req.save()

    tecnicos = User.objects.filter(
        profile__rol=UserProfile.Rol.SOLICITANTE,
        profile__sede_principal=sede,
        is_active=True
    ).order_by('username')

    return render(request, 'inventario/req_asignacion_directa.html', {
        'req': req,
        'tecnicos': tecnicos,
        'sede': sede
    })

@login_required
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
def almacen_devolucion_rapida(request):
    """
    Vista para que Almacén registre devoluciones rápidas de técnicos.
    - Sube stock en Almacén.
    - Baja stock en Mochila del Técnico.
    - Libera seriales (ONUs).
    """
    profile = get_profile(request.user)
    sede = profile.get_sede_operativa()
    
    # Obtener técnicos de la misma sede
    tecnicos = User.objects.filter(
        profile__rol=UserProfile.Rol.SOLICITANTE,
        profile__sede_principal=sede,
        is_active=True
    ).order_by('username')

    if request.method == 'POST':
        tecnico_id = request.POST.get('tecnico_id')
        notas = request.POST.get('notas', '')
        
        # Datos dinámicos del formulario
        productos_ids = request.POST.getlist('productos[]')
        cantidades = request.POST.getlist('cantidades[]')
        seriales_json = request.POST.getlist('seriales_json[]') # Lista de JSON strings para seriales

        if not tecnico_id or not productos_ids:
            messages.error(request, "Datos incompletos.")
            return redirect('almacen_devolucion_rapida')

        tecnico = get_object_or_404(User, id=tecnico_id)

        try:
            with transaction.atomic():
                # 1. Crear Documento ING (Ingreso por Devolución)
                ing = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.ING,
                    estado=EstadoDocumento.CONFIRMADO,
                    sede=sede,
                    responsable=request.user, # Almacenero recibe
                    entregado_por=tecnico,    # Técnico entrega
                    referencia="DEVOLUCION-RAPIDA",
                    observaciones=notas,
                    fecha=timezone.now()
                )
                ing.asignar_numero_si_falta()

                # 2. Procesar ítems
                for i, prod_id in enumerate(productos_ids):
                    producto = get_object_or_404(Producto, id=prod_id)
                    qty = int(cantidades[i])
                    json_data = seriales_json[i] if i < len(seriales_json) else ""

                    if qty > 0:
                        # A. Registrar Item en Documento
                        DocumentoItem.objects.create(
                            documento=ing,
                            producto=producto,
                            cantidad=qty,
                            observacion="Devolución"
                        )

                        # B. Mover Stock Físico (Sube Almacén)
                        MovimientoInventario.objects.create(
                            producto=producto,
                            sede=sede,
                            tipo=MovimientoInventario.TIPO_IN,
                            qty=qty,
                            referencia=ing.numero,
                            usuario=request.user,
                            nota=f"Devolución de {tecnico.username}"
                        ).aplicar()

                        # C. Ajustar Mochila Técnico (Baja su deuda)
                        # Solo si es consumible o activo (ambos se descuentan al devolver)
                        stock_tech = StockTecnico.objects.filter(tecnico=tecnico, producto=producto).first()
                        if stock_tech:
                            stock_tech.cantidad = max(0, stock_tech.cantidad - qty)
                            if stock_tech.cantidad == 0 and not producto.es_activo:
                                stock_tech.delete() # Limpiamos basura si es consumible y llega a 0
                            else:
                                stock_tech.save()

                        # D. Lógica de Seriales (Liberar ONUs)
                        if producto.es_serializado and json_data:
                            import json
                            try:
                                series_list = json.loads(json_data) # [{'sn': '...', 'mac': '...'}]
                                for s_data in series_list:
                                    sn = s_data.get('sn', '').strip().upper()
                                    if sn:
                                        # Buscar el ítem (debería estar ASIGNADO al técnico o en estado ASIGNADO)
                                        # Buscamos por SN globalmente
                                        item_serial = ItemSerializado.objects.filter(serial=sn).first()
                                        
                                        if item_serial:
                                            # Lo devolvemos al almacén
                                            item_serial.estado = ItemSerializado.Estado.EN_ALMACEN
                                            item_serial.asignado_a = None
                                            item_serial.ubicacion = Ubicacion.objects.filter(sede=sede).first() # Ubicación por defecto
                                            item_serial.save()
                            except:
                                pass # Si falla el JSON, no rompemos todo, pero el stock sí cuadra

                messages.success(request, f"✅ Devolución registrada correctamente ({ing.numero}).")
                return redirect('dash_almacen')

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

    return render(request, 'inventario/devolucion_form.html', {
        'tecnicos': tecnicos,
        'sede': sede
    })

@login_required
def devolucion_print(request, doc_id: int):
    """
    Imprime la constancia de devolución (Documento ING).
    """
    doc = get_object_or_404(DocumentoInventario, id=doc_id, tipo=TipoDocumento.ING)
    
    # Validamos que sea una devolución
    if "DEVOLUCION" not in (doc.referencia or "").upper():
         messages.error(request, "Este documento no es una devolución de técnico.")
         return redirect("almacen_historial_global")

    items = doc.items.select_related("producto").order_by("producto__nombre")
    total_cantidad = sum(int(it.cantidad) for it in items)

    return render(request, "inventario/pdf_devolucion.html", {
        "doc": doc,
        "items": items,
        "total_cantidad": total_cantidad,
        "fecha_impresion": timezone.now(),
        "usuario": request.user,
    })

@require_POST
@login_required
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
def req_scan_directo(request):
    try:
        codigo_escaneado = normalizar_codigo_barras(request.POST.get("codigo_escaneado") or "")
        tecnico_id = request.POST.get('tecnico_id') 
        
        if not codigo_escaneado or not tecnico_id:
            return JsonResponse({"ok": False, "error": "Faltan datos de escaneo o no hay técnico seleccionado."})

        tecnico = get_object_or_404(User, id=tecnico_id)
        producto_obj = None

        serial_encontrado = ItemSerializado.objects.filter(
            Q(serial__iexact=codigo_escaneado) |
            Q(mac_address__iexact=codigo_escaneado) |
            Q(serial_secundario__iexact=codigo_escaneado) |
            Q(codigo_trazabilidad__iexact=codigo_escaneado)
        ).first()

        if serial_encontrado:
            if serial_encontrado.estado != ItemSerializado.Estado.EN_ALMACEN: 
                return JsonResponse({"ok": False, "error": f"Este equipo está {serial_encontrado.estado}."})
            producto_obj = serial_encontrado.producto
        else:
            producto_obj = Producto.objects.filter(
                Q(barcode__iexact=codigo_escaneado) | Q(codigo_interno__iexact=codigo_escaneado)
            ).first()

        if producto_obj:
            try:
                # 🛡️ Ponemos la protección aquí
                ubicacion = _get_ubicacion_operativa(request.user)
            except ValidationError as ve:
                return JsonResponse({"ok": False, "error": f"Configuración incompleta: {str(ve)}"})

            req_borrador = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)

            stock_obj = Stock.objects.filter(
                producto=producto_obj,
                sede=ubicacion.sede
            ).first()

            stock_disponible = stock_obj.cantidad if stock_obj else 0

            item_actual = req_borrador.items.filter(producto=producto_obj).first()
            cantidad_en_mochila = item_actual.cantidad if item_actual else 0

            if cantidad_en_mochila + 1 > stock_disponible:
                return JsonResponse({
                    "ok": False,
                    "error": (
                        f"No hay stock suficiente de {producto_obj.nombre}. "
                        f"Disponible: {stock_disponible}, en mochila: {cantidad_en_mochila}."
                    )
                })

            if producto_obj.es_serializado:
                if not serial_encontrado:
                    return JsonResponse({
                        "ok": False,
                        "error": (
                            f"{producto_obj.nombre} es serializado. "
                            "Escanea el GPON SN, MAC, EN/D-SN o código pintado del equipo."
                        )
                    })

                if serial_encontrado.estado != ItemSerializado.Estado.EN_ALMACEN:
                    return JsonResponse({
                        "ok": False,
                        "error": f"Este equipo está {serial_encontrado.get_estado_display()}."
                    })

                if item_actual and DocumentoItemSerializado.objects.filter(
                    documento_item=item_actual,
                    item_serializado=serial_encontrado
                ).exists():
                    return JsonResponse({
                        "ok": False,
                        "error": f"El equipo {serial_encontrado.serial} ya está en la mochila."
                    })
            
            add_item_to_req(
                user=request.user, 
                req=req_borrador, 
                producto=producto_obj, 
                cantidad=1
            )

            item_actual = req_borrador.items.get(producto=producto_obj)

            if serial_encontrado:
                DocumentoItemSerializado.objects.get_or_create(
                    documento_item=item_actual,
                    item_serializado=serial_encontrado
                )
            
            items_data = []

            for item in req_borrador.items.select_related("producto").all():
                stock_item = Stock.objects.filter(
                    producto=item.producto,
                    sede=ubicacion.sede
                ).first()

                disponible = stock_item.cantidad if stock_item else 0

                seriales = []

                if item.producto.es_serializado:
                    seriales = [
                        {
                            "id": s.item_serializado.id,
                            "serial": s.item_serializado.serial or "",
                            "mac": s.item_serializado.mac_address or "",
                            "serial_secundario": s.item_serializado.serial_secundario or "",
                            "codigo_trazabilidad": s.item_serializado.codigo_trazabilidad or "",
                        }
                        for s in item.seriales_seleccionados.select_related("item_serializado").all()
                    ]

                items_data.append({
                    "producto_id": item.producto.id,
                    "nombre": item.producto.nombre,
                    "codigo": item.producto.codigo_interno,
                    "cantidad": item.cantidad,
                    "unidad": item.producto.unidad,
                    "disponible": disponible,
                    "es_serializado": item.producto.es_serializado,
                    "seriales": seriales,
                })
            
            return JsonResponse({"ok": True, "items": items_data, "mensaje": f"Agregado. Destino: {tecnico.get_full_name() or tecnico.username}"})

        return JsonResponse({"ok": False, "error": f"Código '{codigo_escaneado}' no reconocido."})
    
    except Exception as e:
        # 🛡️ Devolvemos 400 para que JS lea el mensaje y NO se vaya al catch()
        return JsonResponse({"ok": False, "error": f"Error interno: {str(e)}"}, status=400)