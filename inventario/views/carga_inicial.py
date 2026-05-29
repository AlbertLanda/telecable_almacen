from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import redirect, render, get_object_or_404
from django.views.decorators.http import require_POST
from django.db.models import Sum, Count

from inventario.models import (
    Categoria,
    MovimientoInventario,
    Producto,
    Sede,
    Stock,
    Ubicacion,
    UserProfile,
    CierreCargaInicial,
    ItemSerializado,
)

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

def _require_almacen(user):
    profile = getattr(user, "profile", None)

    if not profile:
        raise PermissionDenied("Usuario sin perfil.")

    if profile.rol not in (
        UserProfile.Rol.ALMACEN,
        UserProfile.Rol.ADMIN,
        UserProfile.Rol.JEFA,
    ):
        raise PermissionDenied("No tienes permisos para carga inicial.")

    sede = profile.get_sede_operativa()

    if not sede:
        raise PermissionDenied("No tienes sede operativa asignada.")

    return profile, sede


@login_required
def carga_inicial_home(request):
    profile, sede = _require_almacen(request.user)

    q = normalizar_codigo_barras(request.GET.get("q") or "")

    producto = None
    stock_actual = None

    if q:
        producto = Producto.objects.filter(
            Q(codigo_interno=q) | Q(barcode=q)
        ).first()

        if producto:
            stock_actual = Stock.objects.filter(
                producto=producto,
                sede=sede
            ).first()
        else:
            messages.warning(
                request,
                f"No se encontró producto con código: {q}. Puedes registrarlo como nuevo."
            )

    categorias = Categoria.objects.all().order_by("nombre")
    ubicaciones = Ubicacion.objects.filter(sede=sede).order_by("nombre")

    stocks = (
        Stock.objects.filter(sede=sede)
        .select_related("producto", "producto__categoria")
        .order_by("producto__nombre")
    )

    cierre_carga = CierreCargaInicial.objects.filter(sede=sede).first()

    return render(request, "inventario/carga_inicial/home.html", {
        "profile": profile,
        "sede": sede,
        "q": q,
        "producto": producto,
        "stock_actual": stock_actual,
        "categorias": categorias,
        "ubicaciones": ubicaciones,
        "stocks": stocks,
        "cierre_carga": cierre_carga,
    })


@require_POST
@login_required
@transaction.atomic
def carga_inicial_registrar_existente(request):
    profile, sede = _require_almacen(request.user)
    
    if CierreCargaInicial.objects.filter(sede=sede).exists():
        messages.error(request, "La carga inicial de esta sede ya fue cerrada. No se pueden agregar más productos.")
        return redirect("carga_inicial_home")

    producto_id = request.POST.get("producto_id")
    cantidad = int(request.POST.get("cantidad") or 0)
    ubicacion_id = request.POST.get("ubicacion_id") or None
    nota = (request.POST.get("nota") or "").strip()

    

    if cantidad <= 0:
        messages.error(request, "La cantidad debe ser mayor a 0.")
        return redirect("carga_inicial_home")

    producto = Producto.objects.get(id=producto_id)

    ubicacion = None
    if ubicacion_id:
        ubicacion = Ubicacion.objects.filter(id=ubicacion_id, sede=sede).first()

    mov = MovimientoInventario.objects.create(
        producto=producto,
        sede=sede,
        ubicacion=ubicacion,
        tipo=MovimientoInventario.TIPO_IN,
        qty=cantidad,
        referencia=f"CARGA-INICIAL-{sede.nombre.upper()}",
        usuario=request.user,
        nota=nota or "Carga inicial de inventario",
    )
    mov.aplicar()

    messages.success(
        request,
        f"Se cargó {cantidad} de {producto.nombre} al stock inicial de {sede.nombre}."
    )

    return redirect("carga_inicial_home")


@require_POST
@login_required
@transaction.atomic
def carga_inicial_registrar_nuevo(request):
    profile, sede = _require_almacen(request.user)
    if CierreCargaInicial.objects.filter(sede=sede).exists():
        messages.error(request, "La carga inicial de esta sede ya fue cerrada. No se pueden agregar más productos.")
        return redirect("carga_inicial_home")

    nombre = (request.POST.get("nombre") or "").strip()
    categoria_id = request.POST.get("categoria_id") or None
    barcode = normalizar_codigo_barras(request.POST.get("barcode") or "") or None
    unidad = (request.POST.get("unidad") or "UND").strip().upper()
    costo_unitario = Decimal(request.POST.get("costo_unitario") or "0.00")
    stock_minimo = int(request.POST.get("stock_minimo") or 0)
    cantidad = int(request.POST.get("cantidad") or 0)
    ubicacion_id = request.POST.get("ubicacion_id") or None

    es_serializado = bool(request.POST.get("es_serializado"))
    es_activo = bool(request.POST.get("es_activo"))

    if not nombre:
        messages.error(request, "El nombre del producto es obligatorio.")
        return redirect("carga_inicial_home")

    if cantidad <= 0:
        messages.error(request, "La cantidad inicial debe ser mayor a 0.")
        return redirect("carga_inicial_home")

    categoria = None
    if categoria_id:
        categoria = Categoria.objects.filter(id=categoria_id).first()

    ubicacion = None
    if ubicacion_id:
        ubicacion = Ubicacion.objects.filter(id=ubicacion_id, sede=sede).first()

    producto = Producto(
        nombre=nombre,
        categoria=categoria,
        barcode=barcode,
        unidad=unidad,
        costo_unitario=costo_unitario,
        stock_minimo=stock_minimo,
        es_serializado=es_serializado,
        es_activo=es_activo,
        activo=True,
    )

    try:
        producto.full_clean()
        producto.save()
    except ValidationError as e:
        messages.error(request, f"No se pudo registrar el producto: {e}")
        return redirect("carga_inicial_home")

    mov = MovimientoInventario.objects.create(
        producto=producto,
        sede=sede,
        ubicacion=ubicacion,
        tipo=MovimientoInventario.TIPO_IN,
        qty=cantidad,
        referencia=f"CARGA-INICIAL-{sede.nombre.upper()}",
        usuario=request.user,
        nota="Producto creado desde carga inicial",
    )
    mov.aplicar()

    messages.success(
        request,
        f"Producto {producto.nombre} creado con código {producto.codigo_interno} y stock inicial {cantidad}."
    )

    return redirect("carga_inicial_home")

@login_required
def producto_etiqueta_print(request, producto_id):
    profile, sede = _require_almacen(request.user)

    producto = Producto.objects.get(id=producto_id)

    stock = Stock.objects.filter(
        producto=producto,
        sede=sede
    ).first()

    try:
        cantidad = int(request.GET.get("cantidad") or 0)
    except ValueError:
        cantidad = 0

    if cantidad <= 0:
        cantidad = stock.cantidad if stock else 1

    if cantidad <= 0:
        cantidad = 1

    etiquetas = range(cantidad)

    return render(request, "inventario/carga_inicial/etiqueta.html", {
        "producto": producto,
        "sede": sede,
        "stock": stock,
        "cantidad": cantidad,
        "etiquetas": etiquetas,
    })

@login_required
def carga_inicial_resumen(request):
    profile, sede = _require_almacen(request.user)

    referencia = f"CARGA-INICIAL-{sede.nombre.upper()}"

    stocks = (
        Stock.objects.filter(sede=sede)
        .select_related("producto", "producto__categoria")
        .order_by("producto__nombre")
    )

    movimientos = (
        MovimientoInventario.objects.filter(
            sede=sede,
            referencia=referencia,
            tipo=MovimientoInventario.TIPO_IN,
        )
        .select_related("producto", "usuario")
        .order_by("-creado_en")
    )

    total_productos = stocks.count()
    total_unidades = stocks.aggregate(total=Sum("cantidad"))["total"] or 0

    total_movimientos = movimientos.count()

    cierre_carga = CierreCargaInicial.objects.filter(sede=sede).first()

    return render(request, "inventario/carga_inicial/resumen.html", {
        "profile": profile,
        "sede": sede,
        "referencia": referencia,
        "stocks": stocks,
        "movimientos": movimientos,
        "total_productos": total_productos,
        "total_unidades": total_unidades,
        "total_movimientos": total_movimientos,
        "cierre_carga": cierre_carga,
    })

@require_POST
@login_required
@transaction.atomic
def carga_inicial_cerrar(request):
    profile, sede = _require_almacen(request.user)

    referencia = f"CARGA-INICIAL-{sede.nombre.upper()}"

    total_stock = Stock.objects.filter(sede=sede).count()

    if total_stock <= 0:
        messages.error(request, "No se puede cerrar la carga inicial porque aún no hay productos cargados.")
        return redirect("carga_inicial_resumen")

    cierre_existente = CierreCargaInicial.objects.filter(sede=sede).first()

    if cierre_existente:
        messages.warning(request, "La carga inicial de esta sede ya estaba cerrada.")
        return redirect("carga_inicial_resumen")

    observaciones = (request.POST.get("observaciones") or "").strip()

    CierreCargaInicial.objects.create(
        sede=sede,
        responsable=request.user,
        referencia=referencia,
        observaciones=observaciones,
    )

    messages.success(
        request,
        f"Carga inicial de {sede.nombre} cerrada correctamente."
    )

    return redirect("carga_inicial_resumen")

@login_required
def carga_inicial_seriales(request, producto_id):
    profile, sede = _require_almacen(request.user)

    producto = get_object_or_404(Producto, id=producto_id)

    if not producto.es_serializado:
        messages.warning(request, "Este producto no está marcado como serializado.")
        return redirect("carga_inicial_home")

    stock = Stock.objects.filter(producto=producto, sede=sede).first()

    ubicaciones = Ubicacion.objects.filter(sede=sede).order_by("nombre")

    seriales = (
        ItemSerializado.objects
        .filter(
            producto=producto,
            ubicacion__sede=sede,
        )
        .select_related("ubicacion")
        .order_by("-creado_en")
    )

    cantidad_stock = stock.cantidad if stock else 0
    cantidad_seriales = seriales.count()
    pendientes = max(cantidad_stock - cantidad_seriales, 0)

    return render(request, "inventario/carga_inicial/seriales.html", {
        "profile": profile,
        "sede": sede,
        "producto": producto,
        "stock": stock,
        "ubicaciones": ubicaciones,
        "seriales": seriales,
        "cantidad_stock": cantidad_stock,
        "cantidad_seriales": cantidad_seriales,
        "pendientes": pendientes,
    })

@require_POST
@login_required
@transaction.atomic
def carga_inicial_serial_registrar(request, producto_id):
    profile, sede = _require_almacen(request.user)

    producto = get_object_or_404(Producto, id=producto_id)

    if not producto.es_serializado:
        messages.error(request, "Este producto no está marcado como serializado.")
        return redirect("carga_inicial_home")

    serial = normalizar_codigo_barras(request.POST.get("serial") or "")
    mac_address = normalizar_codigo_barras(request.POST.get("mac_address") or "")
    serial_secundario = normalizar_codigo_barras(request.POST.get("serial_secundario") or "")
    codigo_trazabilidad = (request.POST.get("codigo_trazabilidad") or "").strip().upper()
    ubicacion_id = request.POST.get("ubicacion_id") or None

    if not serial:
        messages.error(request, "El GPON SN / Serial principal es obligatorio.")
        return redirect("carga_inicial_seriales", producto_id=producto.id)

    if ubicacion_id:
        ubicacion = Ubicacion.objects.filter(id=ubicacion_id, sede=sede).first()
    else:
        ubicacion, _ = Ubicacion.objects.get_or_create(
            sede=sede,
            nombre="GENERAL",
            defaults={"descripcion": "Ubicación general automática"}
        )

    if ItemSerializado.objects.filter(serial=serial).exists():
        messages.error(request, f"Ya existe un equipo registrado con el serial {serial}.")
        return redirect("carga_inicial_seriales", producto_id=producto.id)

    if mac_address and ItemSerializado.objects.filter(mac_address=mac_address).exists():
        messages.error(request, f"Ya existe un equipo registrado con la MAC {mac_address}.")
        return redirect("carga_inicial_seriales", producto_id=producto.id)

    ItemSerializado.objects.create(
        producto=producto,
        serial=serial,
        codigo_trazabilidad=codigo_trazabilidad or None,
        mac_address=mac_address or None,
        serial_secundario=serial_secundario or None,
        ubicacion=ubicacion,
        estado=ItemSerializado.Estado.EN_ALMACEN,
    )

    messages.success(request, f"Equipo serializado {serial} registrado correctamente.")

    return redirect("carga_inicial_seriales", producto_id=producto.id)

@login_required
@transaction.atomic
def carga_inicial_serial_editar(request, item_id):
    profile, sede = _require_almacen(request.user)

    item = get_object_or_404(
        ItemSerializado.objects.select_related("producto", "ubicacion"),
        id=item_id
    )

    producto = item.producto

    # Seguridad: solo permitir editar seriales de la sede operativa
    if item.ubicacion and item.ubicacion.sede_id != sede.id:
        messages.error(request, "Este equipo no pertenece a tu sede operativa.")
        return redirect("carga_inicial_seriales", producto_id=producto.id)

    if item.estado != ItemSerializado.Estado.EN_ALMACEN:
        messages.error(request, "Solo se pueden editar equipos que están en almacén.")
        return redirect("carga_inicial_seriales", producto_id=producto.id)

    ubicaciones = Ubicacion.objects.filter(sede=sede).order_by("nombre")

    if request.method == "POST":
        serial = normalizar_codigo_barras(request.POST.get("serial") or "")
        mac_address = normalizar_codigo_barras(request.POST.get("mac_address") or "")
        serial_secundario = normalizar_codigo_barras(request.POST.get("serial_secundario") or "")
        codigo_trazabilidad = (request.POST.get("codigo_trazabilidad") or "").strip().upper()
        ubicacion_id = request.POST.get("ubicacion_id") or None

        if not serial:
            messages.error(request, "El GPON SN / Serial principal es obligatorio.")
            return redirect("carga_inicial_serial_editar", item_id=item.id)

        if ItemSerializado.objects.exclude(id=item.id).filter(serial__iexact=serial).exists():
            messages.error(request, f"Ya existe otro equipo registrado con el serial {serial}.")
            return redirect("carga_inicial_serial_editar", item_id=item.id)

        if mac_address and ItemSerializado.objects.exclude(id=item.id).filter(mac_address__iexact=mac_address).exists():
            messages.error(request, f"Ya existe otro equipo registrado con la MAC {mac_address}.")
            return redirect("carga_inicial_serial_editar", item_id=item.id)

        ubicacion = None
        if ubicacion_id:
            ubicacion = Ubicacion.objects.filter(id=ubicacion_id, sede=sede).first()

        item.serial = serial
        item.mac_address = mac_address or None
        item.serial_secundario = serial_secundario or None
        item.codigo_trazabilidad = codigo_trazabilidad or None
        item.ubicacion = ubicacion
        item.save()

        messages.success(request, f"Equipo {item.serial} actualizado correctamente.")
        return redirect("carga_inicial_seriales", producto_id=producto.id)

    return render(request, "inventario/carga_inicial/serial_editar.html", {
        "profile": profile,
        "sede": sede,
        "producto": producto,
        "item": item,
        "ubicaciones": ubicaciones,
    })