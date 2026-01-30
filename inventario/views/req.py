from __future__ import annotations

from django.contrib.auth.decorators import login_required
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
from django.db import transaction
from inventario.models import StockTecnico, DocumentoItem, MovimientoInventario

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
    """
    Vista principal inteligente.
    CORREGIDA: Busca agresivamente cualquier borrador (REQ_BORRADOR o BORRADOR)
    antes de intentar crear uno nuevo.
    """
    profile = getattr(request.user, 'profile', None)
    if not profile:
        return redirect('logout')
    
    # 1. Obtener Ubicación Segura
    # Usamos filter().first() para evitar el error 500 si no hay ubicaciones
    ubicacion_obj = Ubicacion.objects.filter(sede=profile.get_sede_operativa()).order_by('nombre').first()
    
    # 2. BUSCAR BORRADOR EXISTENTE (La Corrección Clave 🔑)
    # Buscamos si ya existe uno en cualquiera de los dos estados posibles
    # Esto evita que se cree uno nuevo al recargar la página
    req_borrador = DocumentoInventario.objects.filter(
        responsable=request.user,
        estado__in=[EstadoDocumento.BORRADOR, EstadoDocumento.REQ_BORRADOR], 
        tipo=TipoDocumento.REQ
    ).first()

    # Solo si NO existe ninguno, dejamos que el servicio cree uno nuevo
    if not req_borrador:
        req_borrador = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion_obj)

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
    return req_home(request)



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
    
    # 2. Validación de Estado (Aceptamos ambos por si acaso)
    if req.estado not in [EstadoDocumento.BORRADOR, EstadoDocumento.REQ_BORRADOR]:
        messages.error(request, "El requerimiento ya fue enviado o procesado.")
        return redirect("req_home")

    # 3. Validar contenido
    if not req.items.exists():
        messages.error(request, "El carrito está vacío.")
        return redirect("req_home")

    # 4. Validar Configuración (Aquí es donde fallaba antes)
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
        req.estado = EstadoDocumento.REQ_PENDIENTE
        req.fecha = timezone.now()
        req.save()
        
        # 🟢 FIX CLAVE: Recargar para obtener el número generado (REQ-000X)
        req.refresh_from_db()

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
    """
    Vista híbrida:
    GET -> Muestra pantalla de confirmación visual (stock vs solicitado).
    POST -> Ejecuta el despacho, genera SAL, descuenta stock y llena mochila.
    """
    req = get_object_or_404(DocumentoInventario, id=req_id, tipo=TipoDocumento.REQ)
    
    if req.estado != EstadoDocumento.REQ_PENDIENTE:
        messages.error(request, "El REQ no está pendiente.")
        return redirect("dash_almacen")

    # --- PROCESAR POST (CONFIRMACIÓN) ---
    if request.method == "POST":
        try:
            with transaction.atomic():
                
                # 🧠 LÓGICA DE SEDES CORREGIDA (PARA ENTRE SEDES) 🧠
                if req.tipo_requerimiento == TipoRequerimiento.ENTRE_SEDES:
                    # Si Oroya pidió a Jauja:
                    # El que despacha (Usuario Actual/Jauja) es el ORIGEN.
                    # El que pidió (req.sede/Oroya) es el DESTINO.
                    sede_salida = request.user.profile.get_sede_operativa()
                    sede_llegada = req.sede
                else:
                    # Si es LOCAL o PROVEEDOR, todo ocurre en la misma sede
                    sede_salida = req.sede
                    sede_llegada = None

                # 1. Crear SALIDA
                sal = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.SAL,
                    estado=EstadoDocumento.CONFIRMADO,
                    sede=sede_salida,          # <--- Jauja (De aquí sale el stock)
                    sede_destino=sede_llegada, # <--- Oroya (Aquí debe aparecer el aviso)
                    responsable=request.user,
                    solicitante=req.responsable,
                    origen=req,
                    referencia=f"Atención REQ {req.numero}",
                    fecha=timezone.now(),
                    observaciones=request.POST.get('notas', '')
                )
                sal.asignar_numero_si_falta() # 🚨 Generar ID SAL-000...

                # 2. Procesar Ítems según lo que confirmaste en el form
                items_req = req.items.all()
                hubo_despacho = False

                for item in items_req:
                    # Obtenemos la cantidad que el almacenero escribió en el input
                    qty_input = int(request.POST.get(f'qty_{item.id}', 0))

                    if qty_input > 0:
                        # Validar Stock Real (EN SEDE SALIDA)
                        stock_almacen = Stock.objects.filter(producto=item.producto, sede=sede_salida).first()
                        stock_actual = stock_almacen.cantidad if stock_almacen else 0

                        if stock_actual < qty_input:
                            raise ValidationError(f"Stock insuficiente para {item.producto.nombre}. Tienes {stock_actual}, intentas sacar {qty_input}.")

                        # A. Item en Documento SAL
                        DocumentoItem.objects.create(
                            documento=sal,
                            producto=item.producto,
                            cantidad=qty_input,
                            observacion=item.observacion
                        )

                        # B. Movimiento Almacén (Resta física de SEDE SALIDA)
                        mov = MovimientoInventario.objects.create(
                            producto=item.producto,
                            sede=sede_salida, # <--- Resta de Jauja
                            tipo=MovimientoInventario.TIPO_OUT,
                            qty=qty_input,
                            referencia=sal.numero,
                            usuario=request.user,
                            nota=f"Traspaso a {sede_llegada.nombre if sede_llegada else 'Local'}"
                        )
                        mov.aplicar()

                        # C. Mochila Técnico (SOLO SI ES LOCAL)
                        # Si es traspaso entre sedes, NO se llena mochila, se espera recepción en la otra sede.
                        if req.tipo_requerimiento == TipoRequerimiento.LOCAL:
                            from inventario.models import StockTecnico
                            stock_tech, _ = StockTecnico.objects.get_or_create(
                                tecnico=req.responsable,
                                producto=item.producto
                            )
                            stock_tech.cantidad += qty_input
                            stock_tech.save()
                        
                        hubo_despacho = True

                if hubo_despacho:
                    req.estado = EstadoDocumento.REQ_ATENDIDO
                    req.save()
                    messages.success(request, f"✅ Despacho {sal.numero} realizado con éxito.")
                    return redirect("sal_detail", sal_id=sal.id)
                else:
                    messages.warning(request, "⚠️ No se despachó nada (cantidades en 0).")
                    sal.delete() # Borrar SAL vacía
                    return redirect("req_convert_to_sal", req_id=req.id)

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")
            return redirect("req_convert_to_sal", req_id=req.id)

    # --- PROCESAR GET (MOSTRAR PANTALLA) ---
    # Usamos la sede del usuario actual (Jauja) para ver SU stock disponible
    sede_visualizar = request.user.profile.get_sede_operativa()

    items_procesados = []
    for item in req.items.select_related('producto').all():
        stock_obj = Stock.objects.filter(producto=item.producto, sede=sede_visualizar).first()
        stock_actual = stock_obj.cantidad if stock_obj else 0
        
        # Sugerimos despachar lo que pide, o lo que hay si es menos
        sugerido = min(item.cantidad, stock_actual)

        items_procesados.append({
            'original_id': item.id,
            'producto': item.producto,
            'cantidad_solicitada': item.cantidad,
            'stock_actual': stock_actual,
            'cantidad_sugerida': sugerido
        })

    return render(request, 'inventario/req_despachar_form.html', {
        'req': req,
        'items_procesados': items_procesados
    })

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

@login_required
def req_recepcionar_compra(request, req_id: int):
    """
    Convierte un REQ de PROVEEDOR en un INGRESO (ING).
    Sube el stock en la Sede Central.
    """
    req = get_object_or_404(DocumentoInventario, id=req_id, tipo=TipoDocumento.REQ)
    
    # Seguridad: Solo para compras a proveedores
    if req.tipo_requerimiento != TipoRequerimiento.PROVEEDOR:
        messages.error(request, "Este no es un pedido a proveedor.")
        return redirect("dash_almacen")

    if request.method == "POST":
        try:
            with transaction.atomic():
                # 1. Crear el Ingreso (ING)
                ing = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.ING,
                    estado=EstadoDocumento.CONFIRMADO,
                    sede=req.sede,             # Entra a mi sede (Jauja)
                    responsable=request.user,  # Yo lo recibo
                    proveedor=req.proveedor,   # Viene de este proveedor
                    origen=req,                # Basado en este REQ
                    referencia=f"Llegada Compra {req.numero}",
                    fecha=timezone.now(),
                    observaciones=request.POST.get('notas', '')
                )
                ing.asignar_numero_si_falta()

                items_req = req.items.all()
                
                # 2. Procesar ítems
                for item in items_req:
                    # Leemos cuánto llegó realmente (input del form)
                    qty_llegada = int(request.POST.get(f'qty_{item.id}', 0))
                    
                    if qty_llegada > 0:
                        # A. Item en Documento ING
                        DocumentoItem.objects.create(
                            documento=ing,
                            producto=item.producto,
                            cantidad=qty_llegada,
                            observacion=item.observacion
                        )

                        # B. Movimiento Físico (SUMA STOCK) 📈
                        mov = MovimientoInventario.objects.create(
                            producto=item.producto,
                            sede=req.sede,
                            tipo=MovimientoInventario.TIPO_IN, # Entrada
                            qty=qty_llegada,
                            referencia=ing.numero,
                            usuario=request.user,
                            nota=f"Compra a {req.proveedor.razon_social if req.proveedor else 'Proveedor'}"
                        )
                        mov.aplicar()

                # 3. Cerrar el REQ
                req.estado = EstadoDocumento.REQ_ATENDIDO
                req.save()

                messages.success(request, f"✅ Compra ingresada al stock con éxito ({ing.numero}).")
                return redirect("dash_almacen")

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")
            return redirect("dash_almacen")

    # --- GET: Mostrar formulario de verificación (Igual que el de despacho pero para recibir) ---
    return render(request, 'inventario/req_recepcionar_form.html', {'req': req})

@require_POST
@login_required
def req_set_tipo(request):
    """
    Guarda la configuración del REQ (Paso 1).
    Versión final: Limpia, segura y busca en ambos estados.
    """
    try:
        # 1. RECIBIR EL ID EXACTO (Prioridad absoluta)
        req_id = request.POST.get("req_id")
        req_borrador = None
        
        # A) Si el frontend manda ID, usamos ese
        if req_id:
            req_borrador = DocumentoInventario.objects.filter(id=req_id, responsable=request.user).first()
        
        # B) Si no hay ID o no se encontró, buscamos el activo (Fallback inteligente)
        if not req_borrador:
            req_borrador = DocumentoInventario.objects.filter(
                responsable=request.user,
                estado__in=[EstadoDocumento.BORRADOR, EstadoDocumento.REQ_BORRADOR],
                tipo=TipoDocumento.REQ
            ).first()

        if not req_borrador:
            return JsonResponse({"ok": False, "error": "No se encontró ningún borrador activo para guardar."})

        # 2. PROCESAR DATOS
        tipo = request.POST.get("tipo_requerimiento")
        
        if tipo == "PROVEEDOR":
            nombre_prov = request.POST.get("proveedor_manual", "").strip()
            
            # Validación simple
            if not nombre_prov:
                return JsonResponse({"ok": False, "error": "El nombre del proveedor no puede estar vacío."})
            
            # Asignar valores
            req_borrador.tipo_requerimiento = TipoRequerimiento.PROVEEDOR
            req_borrador.proveedor_manual = nombre_prov 
            req_borrador.proveedor = None 
            req_borrador.sede_destino = None
            
            # 3. GUARDAR (Sin argumentos para asegurar escritura total)
            req_borrador.save()

        elif tipo == "ENTRE_SEDES":
            destino_id = request.POST.get("sede_destino_id")
            if not destino_id:
                return JsonResponse({"ok": False, "error": "Falta seleccionar la sede destino."})
            
            req_borrador.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
            req_borrador.sede_destino_id = destino_id
            req_borrador.proveedor_manual = None
            
            # 3. GUARDAR
            req_borrador.save()

        return JsonResponse({"ok": True})

    except Exception as e:
        # En caso de error inesperado, lo enviamos al frontend
        return JsonResponse({"ok": False, "error": f"Error interno: {str(e)}"})