from django.contrib.auth.decorators import login_required
from django.shortcuts import render, redirect
from django.db.models import Q
from django.http import HttpResponse
from django.utils import timezone

import xlsxwriter
from io import BytesIO

from inventario.models import ItemSerializado, UserProfile, Producto, Sede


def _puede_ver_equipos(user):
    profile = getattr(user, "profile", None)
    if not profile:
        return False

    return profile.rol in [
        UserProfile.Rol.ALMACEN,
        UserProfile.Rol.ADMIN,
        UserProfile.Rol.JEFA,
    ]


def _query_equipos_serializados(request):
    q = (request.GET.get("q") or "").strip()
    estado = (request.GET.get("estado") or "").strip()
    producto_id = (request.GET.get("producto") or "").strip()
    sede_id = (request.GET.get("sede") or "").strip()
    solo_disponibles = (request.GET.get("solo_disponibles") or "").strip()

    equipos = (
        ItemSerializado.objects
        .select_related("producto", "ubicacion", "ubicacion__sede", "asignado_a")
        .all()
        .order_by("producto__nombre", "serial")
    )

    if q:
        equipos = equipos.filter(
            Q(producto__nombre__icontains=q) |
            Q(serial__icontains=q) |
            Q(mac_address__icontains=q) |
            Q(serial_secundario__icontains=q) |
            Q(codigo_trazabilidad__icontains=q) |
            Q(asignado_a__username__icontains=q) |
            Q(asignado_a__first_name__icontains=q) |
            Q(asignado_a__last_name__icontains=q)
        )

    if estado:
        equipos = equipos.filter(estado=estado)

    if producto_id:
        equipos = equipos.filter(producto_id=producto_id)

    if sede_id:
        equipos = equipos.filter(ubicacion__sede_id=sede_id)

    if solo_disponibles == "1":
        equipos = equipos.filter(estado=ItemSerializado.Estado.EN_ALMACEN)

    return equipos


@login_required
def equipos_serializados_list(request):
    if not _puede_ver_equipos(request.user):
        return redirect("home")

    equipos = _query_equipos_serializados(request)

    productos = (
        Producto.objects
        .filter(items_serializados__isnull=False)
        .distinct()
        .order_by("nombre")
    )

    sedes = Sede.objects.filter(activo=True).order_by("nombre")

    context = {
        "equipos": equipos[:1000],
        "q": request.GET.get("q", ""),
        "estado": request.GET.get("estado", ""),
        "producto_seleccionado": request.GET.get("producto", ""),
        "sede_seleccionada": request.GET.get("sede", ""),
        "solo_disponibles": request.GET.get("solo_disponibles", ""),
        "estados": ItemSerializado.Estado.choices,
        "productos": productos,
        "sedes": sedes,
        "total": equipos.count(),
    }

    return render(request, "inventario/equipos_serializados_list.html", context)


@login_required
def equipos_serializados_export(request):
    if not _puede_ver_equipos(request.user):
        return redirect("home")

    equipos = _query_equipos_serializados(request)

    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    worksheet = workbook.add_worksheet("Equipos serializados")

    header_format = workbook.add_format({
        "bold": True,
        "bg_color": "#D9EAF7",
        "border": 1,
    })

    cell_format = workbook.add_format({
        "border": 1,
    })

    headers = [
        "Producto",
        "Serial principal / GPON SN",
        "MAC",
        "DSN / Serial secundario",
        "Código trazabilidad",
        "Estado",
        "Sede / Ubicación",
        "Asignado a",
        "Fecha registro",
    ]

    for col, header in enumerate(headers):
        worksheet.write(0, col, header, header_format)

    for row, item in enumerate(equipos, start=1):
        ubicacion = ""
        if item.ubicacion:
            sede = item.ubicacion.sede.nombre if item.ubicacion.sede else ""
            ubicacion = f"{sede} - {item.ubicacion.nombre}"

        asignado = ""
        if item.asignado_a:
            asignado = item.asignado_a.get_full_name() or item.asignado_a.username

        worksheet.write(row, 0, item.producto.nombre, cell_format)
        worksheet.write(row, 1, item.serial or "", cell_format)
        worksheet.write(row, 2, item.mac_address or "", cell_format)
        worksheet.write(row, 3, item.serial_secundario or "", cell_format)
        worksheet.write(row, 4, item.codigo_trazabilidad or "", cell_format)
        worksheet.write(row, 5, item.get_estado_display(), cell_format)
        worksheet.write(row, 6, ubicacion, cell_format)
        worksheet.write(row, 7, asignado, cell_format)
        worksheet.write(row, 8, item.creado_en.strftime("%d/%m/%Y %H:%M"), cell_format)

    worksheet.set_column(0, 0, 30)
    worksheet.set_column(1, 4, 24)
    worksheet.set_column(5, 5, 18)
    worksheet.set_column(6, 7, 30)
    worksheet.set_column(8, 8, 20)

    workbook.close()
    output.seek(0)

    filename = f"equipos_serializados_{timezone.now().strftime('%Y%m%d_%H%M')}.xlsx"

    response = HttpResponse(
        output.read(),
        content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
    response["Content-Disposition"] = f'attachment; filename="{filename}"'

    return response