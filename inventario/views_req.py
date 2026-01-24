from __future__ import annotations

from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect, get_object_or_404
from django.core.exceptions import ValidationError, PermissionDenied
from django.contrib import messages
from django.views.decorators.http import require_POST
from django.http import JsonResponse
from django.db.models import Q
from inventario.models import Proveedor

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


def _get_sede_central():
    return (
        Sede.objects
        .filter(tipo=Sede.CENTRAL, activo=True)
        .order_by("nombre")
        .first()
    )


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
    """
    Normaliza REQ (sobre todo data antigua) para que no choque con clean().

    Reglas:
    - SOLICITANTE: siempre LOCAL (sin proveedor, sin sede_destino)
    - ALMACEN:
        - CENTRAL: PROVEEDOR (sin sede_destino) (proveedor se setea antes de enviar)
        - SECUNDARIO: ENTRE_SEDES (sede_destino=CENTRAL) y sin proveedor
    - JEFA/ADMIN: no forzamos, pero si viene vacío => LOCAL
    """
    changed = False
    profile = getattr(user, "profile", None)
    if not profile:
        return

    # Si viene vacío/null por data antigua, ponemos algo seguro
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
            # proveedor puede quedar null en borrador (se exige al enviar por clean/full_clean)
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

    # JEFA/ADMIN: si es LOCAL, limpiar campos por seguridad
    else:
        if req.tipo_requerimiento == TipoRequerimiento.LOCAL:
            if req.sede_destino_id:
                req.sede_destino = None
                changed = True
            if getattr(req, "proveedor_id", None):
                req.proveedor = None
                changed = True

    if changed:
        fields = ["tipo_requerimiento", "sede_destino"]
        # proveedor puede no existir en tu modelo viejo, por eso getattr
        if hasattr(req, "proveedor"):
            fields.append("proveedor")
        req.save(update_fields=fields)


# --------------------
# Vistas REQ
# --------------------
@login_required
def req_home(request):
    try:
        _require_roles(
            request.user,
            UserProfile.Rol.SOLICITANTE,
            UserProfile.Rol.JEFA,
            UserProfile.Rol.ALMACEN,   # ✅ AGREGAR
        )
        ubicacion = _get_ubicacion_operativa(request.user)
        sede = _get_sede_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
        return redirect("/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req, request.user)

    sedes_central = Sede.objects.filter(tipo=Sede.CENTRAL, activo=True).order_by("nombre")
    proveedores = Proveedor.objects.filter(activo=True).order_by("razon_social")

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
            "proveedores": proveedores,  # por si tu template lo usa
        },
    )



@require_POST
@login_required
def req_set_tipo_requerimiento(request):
    """
    POST: setear tipo_requerimiento y (opcional) sede_destino/proveedor del REQ borrador.

    Reglas:
    - SOLICITANTE: SOLO LOCAL
    - ALMACEN:
        * sede CENTRAL: PROVEEDOR (requiere proveedor)
        * sede NO CENTRAL: ENTRE_SEDES (requiere sede_destino CENTRAL)
    - JEFA/ADMIN:
        * LOCAL
        * ENTRE_SEDES (requiere sede_destino CENTRAL)
        * PROVEEDOR (solo si REQ es de sede CENTRAL + requiere proveedor)
    """
    try:
        profile = _require_roles(
            request.user,
            UserProfile.Rol.SOLICITANTE,
            UserProfile.Rol.JEFA,
            UserProfile.Rol.ALMACEN,
        )
        ubicacion = _get_ubicacion_operativa(request.user)
        sede_user = _get_sede_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": str(e)}, status=403)
        messages.error(request, str(e))
        return redirect("/req/")

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req, request.user)

    # asegurar sede en el borrador
    if req.tipo == TipoDocumento.REQ and not req.sede_id and sede_user:
        req.sede = sede_user
        req.save(update_fields=["sede"])

    tipo = (request.POST.get("tipo_requerimiento") or "").strip().upper()
    sede_destino_id = (request.POST.get("sede_destino_id") or "").strip()
    proveedor_id = (request.POST.get("proveedor_id") or "").strip()

    # -------------------------
    # SOLICITANTE => siempre LOCAL
    # -------------------------
    if profile.rol == UserProfile.Rol.SOLICITANTE:
        req.tipo_requerimiento = TipoRequerimiento.LOCAL
        req.sede_destino = None
        if hasattr(req, "proveedor"):
            req.proveedor = None
            req.save(update_fields=["tipo_requerimiento", "sede_destino", "proveedor"])
        else:
            req.save(update_fields=["tipo_requerimiento", "sede_destino"])

        if _is_ajax(request):
            return JsonResponse({"ok": True, "tipo_requerimiento": req.tipo_requerimiento})
        messages.success(request, "Tipo actualizado a LOCAL.")
        return redirect("/req/")

    # -------------------------
    # ALMACEN => reglas por sede
    # -------------------------
    if profile.rol == UserProfile.Rol.ALMACEN:
        # NO CENTRAL: solo ENTRE_SEDES
        if sede_user.tipo != Sede.CENTRAL:
            if tipo != TipoRequerimiento.ENTRE_SEDES:
                msg = "Como ALMACÉN (sede no CENTRAL) solo puedes crear REQ ENTRE_SEDES hacia CENTRAL."
                return JsonResponse({"ok": False, "error": msg}, status=400) if _is_ajax(request) else redirect("/req/")

            if not sede_destino_id:
                msg = "Selecciona una sede CENTRAL destino."
                return JsonResponse({"ok": False, "error": msg}, status=400) if _is_ajax(request) else redirect("/req/")

            sede_destino = get_object_or_404(Sede, id=sede_destino_id, activo=True)
            if sede_destino.tipo != Sede.CENTRAL:
                msg = "El destino de ENTRE_SEDES debe ser CENTRAL."
                return JsonResponse({"ok": False, "error": msg}, status=400) if _is_ajax(request) else redirect("/req/")

            req.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
            req.sede_destino = sede_destino
            if hasattr(req, "proveedor"):
                req.proveedor = None
                req.save(update_fields=["tipo_requerimiento", "sede_destino", "proveedor"])
            else:
                req.save(update_fields=["tipo_requerimiento", "sede_destino"])

        # CENTRAL: solo PROVEEDOR
        else:
            if tipo != TipoRequerimiento.PROVEEDOR:
                msg = "Como ALMACÉN CENTRAL solo puedes crear REQ a PROVEEDOR."
                return JsonResponse({"ok": False, "error": msg}, status=400) if _is_ajax(request) else redirect("/req/")

            if not proveedor_id:
                msg = "Selecciona un proveedor para REQ a PROVEEDOR."
                return JsonResponse({"ok": False, "error": msg}, status=400) if _is_ajax(request) else redirect("/req/")

            proveedor = get_object_or_404(Proveedor, id=proveedor_id, activo=True)
            req.tipo_requerimiento = TipoRequerimiento.PROVEEDOR
            req.sede_destino = None
            if hasattr(req, "proveedor"):
                req.proveedor = proveedor
                req.save(update_fields=["tipo_requerimiento", "sede_destino", "proveedor"])
            else:
                req.save(update_fields=["tipo_requerimiento", "sede_destino"])

        # validar modelo
        try:
            req.full_clean()
        except ValidationError as e:
            if _is_ajax(request):
                return JsonResponse({"ok": False, "error": str(e)}, status=400)
            messages.error(request, str(e))
            return redirect("/req/")

        return JsonResponse({"ok": True, "tipo_requerimiento": req.tipo_requerimiento}) if _is_ajax(request) else redirect("/req/")

    # -------------------------
    # JEFA/ADMIN => como lo tenías
    # -------------------------
    if tipo not in (TipoRequerimiento.LOCAL, TipoRequerimiento.PROVEEDOR, TipoRequerimiento.ENTRE_SEDES):
        msg = "Tipo de requerimiento inválido."
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": msg}, status=400)
        messages.error(request, msg)
        return redirect("/req/")

    if tipo == TipoRequerimiento.LOCAL:
        req.tipo_requerimiento = TipoRequerimiento.LOCAL
        req.sede_destino = None
        if hasattr(req, "proveedor"):
            req.proveedor = None
            req.save(update_fields=["tipo_requerimiento", "sede_destino", "proveedor"])
        else:
            req.save(update_fields=["tipo_requerimiento", "sede_destino"])

    elif tipo == TipoRequerimiento.PROVEEDOR:
        if req.sede and req.sede.tipo != Sede.CENTRAL:
            msg = "PROVEEDOR solo aplica si el REQ pertenece a una sede CENTRAL."
            return JsonResponse({"ok": False, "error": msg}, status=400) if _is_ajax(request) else redirect("/req/")

        if not proveedor_id:
            msg = "Selecciona un proveedor para REQ a PROVEEDOR."
            return JsonResponse({"ok": False, "error": msg}, status=400) if _is_ajax(request) else redirect("/req/")

        proveedor = get_object_or_404(Proveedor, id=proveedor_id, activo=True)
        req.tipo_requerimiento = TipoRequerimiento.PROVEEDOR
        req.sede_destino = None
        if hasattr(req, "proveedor"):
            req.proveedor = proveedor
            req.save(update_fields=["tipo_requerimiento", "sede_destino", "proveedor"])
        else:
            req.save(update_fields=["tipo_requerimiento", "sede_destino"])

    else:  # ENTRE_SEDES
        if req.sede and req.sede.tipo == Sede.CENTRAL:
            msg = "La sede CENTRAL no debe generar REQ 'ENTRE SEDES'."
            return JsonResponse({"ok": False, "error": msg}, status=400) if _is_ajax(request) else redirect("/req/")

        if not sede_destino_id:
            msg = "Selecciona una sede CENTRAL destino."
            return JsonResponse({"ok": False, "error": msg}, status=400) if _is_ajax(request) else redirect("/req/")

        sede_destino = get_object_or_404(Sede, id=sede_destino_id, activo=True)
        if sede_destino.tipo != Sede.CENTRAL:
            msg = "El destino de un REQ ENTRE SEDES debe ser CENTRAL."
            return JsonResponse({"ok": False, "error": msg}, status=400) if _is_ajax(request) else redirect("/req/")

        req.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
        req.sede_destino = sede_destino
        if hasattr(req, "proveedor"):
            req.proveedor = None
            req.save(update_fields=["tipo_requerimiento", "sede_destino", "proveedor"])
        else:
            req.save(update_fields=["tipo_requerimiento", "sede_destino"])

    try:
        req.full_clean()
    except ValidationError as e:
        if _is_ajax(request):
            return JsonResponse({"ok": False, "error": str(e)}, status=400)
        messages.error(request, str(e))
        return redirect("/req/")

    if _is_ajax(request):
        return JsonResponse({"ok": True, "tipo_requerimiento": req.tipo_requerimiento})

    messages.success(request, "Tipo de requerimiento actualizado.")
    return redirect("/req/")




@login_required
def req_catalogo(request):
    try:
        _require_roles(
            request.user,
            UserProfile.Rol.SOLICITANTE,
            UserProfile.Rol.JEFA,
            UserProfile.Rol.ALMACEN,
        )
        sede = _get_sede_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=403)

    q = (request.GET.get("q") or "").strip()
    modo = (request.GET.get("modo") or "").strip().lower()

    # ✅ modo proveedor: NO filtrar por stock
    if modo == "proveedor":
        # recomendado: solo CENTRAL
        if sede.tipo != Sede.CENTRAL:
            return JsonResponse(
                {"ok": False, "error": "Solo CENTRAL puede usar catálogo proveedor."},
                status=403,
            )

        productos = Producto.objects.filter(activo=True).order_by("nombre")

        if q:
            productos = productos.filter(
                Q(nombre__icontains=q)
                | Q(codigo_interno__icontains=q)
                | Q(barcode__icontains=q)
            )

        productos = productos[:80]

        data = [{
            "producto_id": p.id,
            "nombre": p.nombre,
            "codigo": (getattr(p, "codigo_interno", "") or getattr(p, "barcode", "") or ""),
            "disponible": None,
            "unidad": getattr(p, "unidad", "") or "",
        } for p in productos]

        return JsonResponse({"ok": True, "modo": "proveedor", "sede": sede.nombre, "results": data})

    # ✅ modo local: filtrar por stock disponible
    stocks = (
        Stock.objects
        .filter(sede=sede, producto__activo=True, cantidad__gt=0)
        .select_related("producto")
        .order_by("producto__nombre")
    )

    if q:
        stocks = stocks.filter(
            Q(producto__nombre__icontains=q)
            | Q(producto__codigo_interno__icontains=q)
            | Q(producto__barcode__icontains=q)
        )

    stocks = stocks[:80]

    data = [{
        "producto_id": s.producto.id,
        "nombre": s.producto.nombre,
        "codigo": getattr(s.producto, "codigo_interno", "") or getattr(s.producto, "barcode", "") or "",
        "disponible": int(s.cantidad or 0),
        "unidad": getattr(s.producto, "unidad", "") or "",
    } for s in stocks]

    return JsonResponse({"ok": True, "modo": "local", "sede": sede.nombre, "results": data}) 

@login_required
def req_carrito(request):
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA, UserProfile.Rol.ALMACEN)
        ubicacion = _get_ubicacion_operativa(request.user)
    except (ValidationError, PermissionDenied) as e:
        return JsonResponse({"ok": False, "error": str(e)}, status=403)

    req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
    _ensure_req_defaults(req, request.user)

    return JsonResponse({
        "ok": True,
        "req_id": req.id,
        "tipo_requerimiento": req.tipo_requerimiento,
        "items": _serialize_cart(req),
    })


@require_POST
@login_required
def req_set_qty(request):
    if not _is_ajax(request):
        return JsonResponse({"ok": False, "error": "Solo AJAX."}, status=400)

    try:
        profile = _require_roles(
            request.user,
            UserProfile.Rol.SOLICITANTE,
            UserProfile.Rol.JEFA,
            UserProfile.Rol.ALMACEN,
        )
        ubicacion = _get_ubicacion_operativa(request.user)
        sede = _get_sede_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
        _ensure_req_defaults(req, request.user)
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
    if not _is_ajax(request):
        return JsonResponse({"ok": False, "error": "Solo AJAX."}, status=400)

    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
        ubicacion = _get_ubicacion_operativa(request.user)
        req = get_or_create_req_borrador(user=request.user, ubicacion=ubicacion)
        _ensure_req_defaults(req, request.user)
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
    _ensure_req_defaults(req, request.user)

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
    _ensure_req_defaults(req, request.user)

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
    _ensure_req_defaults(req, request.user)

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

    # Normaliza por si era data vieja (evita que full_clean reviente por campos mal seteados)
    _ensure_req_defaults(req, request.user)

    # Validaciones “amigables” antes del full_clean (igual full_clean valida todo)
    tipo_req = getattr(req, "tipo_requerimiento", None)

    if tipo_req == TipoRequerimiento.ENTRE_SEDES:
        if not req.sede_destino_id:
            messages.error(request, "Este REQ es ENTRE SEDES: selecciona la sede CENTRAL destino antes de enviar.")
            return redirect("/req/")
        if req.sede_destino and req.sede_destino.tipo != Sede.CENTRAL:
            messages.error(request, "Destino inválido: el destino de ENTRE SEDES debe ser CENTRAL.")
            return redirect("/req/")

    if tipo_req == TipoRequerimiento.PROVEEDOR:
        # proveedor se exige por clean(); aquí damos mensaje claro
        if not getattr(req, "proveedor_id", None):
            messages.error(request, "Este REQ es a PROVEEDOR: selecciona un proveedor antes de enviar.")
            return redirect("/req/")

    if tipo_req == TipoRequerimiento.LOCAL:
        if req.sede_destino_id:
            messages.error(request, "REQ LOCAL no debe tener sede destino.")
            return redirect("/req/")
        if getattr(req, "proveedor_id", None):
            messages.error(request, "REQ LOCAL no debe tener proveedor.")
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
    - LOCAL / PROVEEDOR => inventario/req_print_proveedor.html (sin destino)
    - ENTRE_SEDES       => inventario/req_print_entre_sedes.html
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

        if profile.rol == UserProfile.Rol.SOLICITANTE:
            if req.tipo_requerimiento != TipoRequerimiento.LOCAL or req.sede_destino_id or getattr(req, "proveedor_id", None):
                req.tipo_requerimiento = TipoRequerimiento.LOCAL
                req.sede_destino = None
                if hasattr(req, "proveedor"):
                    req.proveedor = None
                req.save(update_fields=["tipo_requerimiento", "sede_destino"] + (["proveedor"] if hasattr(req, "proveedor") else []))

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

    tipo_req = getattr(req, "tipo_requerimiento", None) or TipoRequerimiento.LOCAL

    if tipo_req == TipoRequerimiento.ENTRE_SEDES:
        template = "inventario/req_print_entre_sedes.html"
    else:
        # LOCAL y PROVEEDOR imprimen “sin destino” (tu template proveedor)
        template = "inventario/req_print_proveedor.html"

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
    - PROVEEDOR solo CENTRAL
    - ENTRE_SEDES requiere sede_destino CENTRAL
    (LOCAL normalmente es para técnico; si lo necesitas aquí, lo añadimos luego)
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

    if tipo == TipoRequerimiento.PROVEEDOR:
        if sede_user.tipo != Sede.CENTRAL:
            messages.error(request, "Solo la sede CENTRAL (Jauja) puede generar requerimientos a PROVEEDOR.")
            return redirect("/dashboard/almacen/")

        req.tipo_requerimiento = TipoRequerimiento.PROVEEDOR
        req.sede_destino = None
        req.save(update_fields=["tipo_requerimiento", "sede_destino"])
        messages.success(request, f"REQ {req.numero or req.id}: tipo actualizado a PROVEEDOR.")
        return redirect("/dashboard/almacen/")

    # ENTRE_SEDES
    if not sede_destino_id:
        messages.error(request, "Para ENTRE SEDES debes seleccionar una sede CENTRAL destino.")
        return redirect("/dashboard/almacen/")

    sede_destino = get_object_or_404(Sede, id=sede_destino_id, activo=True)
    if sede_destino.tipo != Sede.CENTRAL:
        messages.error(request, "El destino para ENTRE SEDES debe ser una sede CENTRAL.")
        return redirect("/dashboard/almacen/")

    req.tipo_requerimiento = TipoRequerimiento.ENTRE_SEDES
    req.sede_destino = sede_destino
    if hasattr(req, "proveedor"):
        req.proveedor = None
        req.save(update_fields=["tipo_requerimiento", "sede_destino", "proveedor"])
    else:
        req.save(update_fields=["tipo_requerimiento", "sede_destino"])

    messages.success(request, f"REQ {req.numero or req.id}: tipo actualizado a ENTRE SEDES.")
    return redirect("/dashboard/almacen/")
