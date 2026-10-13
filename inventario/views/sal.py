from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.utils import timezone
from inventario.permissions import role_required, sede_required, get_profile
from inventario.services.seriales_service import registrar_seriales

from inventario.models import (
    DocumentoInventario, 
    TipoDocumento, 
    UserProfile, 
    EstadoDocumento, 
    MovimientoInventario, 
    DocumentoItem,
    ItemSerializado, # <--- NUEVO IMPORT
    Ubicacion        # <--- NUEVO IMPORT
)


def _require_roles(user, *roles):
    profile = get_profile(user)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")
    return profile


def _sede_operativa(user):
    profile = get_profile(user)
    if not profile:
        raise ValidationError("Usuario sin perfil (UserProfile).")
    sede = profile.get_sede_operativa()
    if not sede:
        raise ValidationError("No tienes sede operativa asignada.")
    return sede


@login_required
def sal_detail(request, sal_id: int):
    sal = get_object_or_404(
        DocumentoInventario.objects.select_related("sede", "responsable", "origen", "ubicacion", "sede_destino", "solicitante"),
        id=sal_id,
        tipo=TipoDocumento.SAL,
    )
    items = list(sal.items.select_related("producto").order_by("producto__nombre"))

    # --- MAGIA ENTERPRISE: INYECTAR SERIALES EN TRÁNSITO ---
    for it in items:
        if it.producto.es_serializado and sal.solicitante:
            seriales = ItemSerializado.objects.filter(
                producto=it.producto,
                documentos_seleccionados__documento_item=it,
            ).values('serial', 'codigo_trazabilidad')

            if seriales:
                it.lista_series = list(seriales)

    try:
        profile = get_profile(request.user)
        if not profile:
            raise PermissionDenied("Usuario sin perfil.")

        allowed = False
        if profile.rol in (UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN):
            allowed = True
        elif profile.rol == UserProfile.Rol.ALMACEN:
            sede_user = _sede_operativa(request.user)
            if sal.sede_id == sede_user.id: 
                allowed = True
            elif sal.sede_destino_id == sede_user.id: 
                allowed = True
        elif profile.rol == UserProfile.Rol.SOLICITANTE:
            allowed = (
                sal.responsable_id == request.user.id
                or (sal.origen_id and sal.origen and sal.origen.responsable_id == request.user.id)
            )

        if not allowed:
            raise PermissionDenied("No tienes permisos para ver esta Guía de Salida.")

        return render(request, "inventario/sal_detail.html", {
            "sal": sal, "items": items,
            "faltan_seriales": any(it.producto.es_serializado and not getattr(it, "lista_series", None) for it in items),
        })

    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/")


@require_POST
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
@transaction.atomic
def sal_confirmar(request, sal_id: int):
    try:
        profile = request.user_profile
        sal = DocumentoInventario.objects.select_for_update().get(
            id=sal_id, tipo=TipoDocumento.SAL
        )

        if profile.rol not in (UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN):
            sede = _sede_operativa(request.user)
            if sal.sede_id != sede.id:
                raise PermissionDenied("No puedes confirmar SAL de otra sede.")

        if sal.estado == EstadoDocumento.CONFIRMADO:
            messages.info(request, f"Esta SAL ya estaba confirmada: {sal.numero}")
            return redirect(f"/sal/{sal_id}/")

        sal.confirmar(entregado_por=request.user)
        messages.success(request, f"SAL confirmada: {sal.numero}")

    except Exception as e:
        messages.error(request, f"Error al confirmar SAL: {e}")

    return redirect(f"/sal/{sal_id}/")


@login_required
def sal_print(request, sal_id: int):
    sal = get_object_or_404(
        DocumentoInventario.objects.select_related("sede", "responsable", "origen", "ubicacion"),
        id=sal_id,
        tipo=TipoDocumento.SAL,
    )
    items = sal.items.select_related("producto").order_by("producto__nombre")
    total_cantidad = sum(int(it.cantidad or 0) for it in items)

    profile = get_profile(request.user)
    if not profile: return redirect("/")
    if profile.rol == UserProfile.Rol.SOLICITANTE:
        if sal.responsable_id != request.user.id:
             if not (sal.origen and sal.origen.responsable_id == request.user.id):
                 return redirect("/")

    return render(request, "inventario/sal_print.html", {
        "sal": sal,
        "items": items,
        "total_cantidad": total_cantidad,
    })


@require_POST
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
@transaction.atomic
def almacen_recepcionar_traspaso(request, sal_id):
    try:
        with transaction.atomic():
            mi_sede = _sede_operativa(request.user)
            sal = get_object_or_404(DocumentoInventario.objects.select_for_update(), id=sal_id, tipo=TipoDocumento.SAL)

            if sal.sede_destino != mi_sede:
                raise PermissionDenied("Este traspaso no es para tu sede.")

            if not mi_sede or sal.sede_id == mi_sede.id or sal.estado != EstadoDocumento.CONFIRMADO:
                raise ValidationError("Se requiere una transferencia confirmada desde otra sede.")
            
            if sal.recibido:
                messages.warning(request, "Este traspaso ya fue recepcionado.")
                return redirect('dash_almacen')

            if DocumentoInventario.objects.filter(tipo=TipoDocumento.ING, origen=sal).exists():
                raise ValidationError("Esta salida ya tiene un ingreso asociado; revisa su recepción antes de continuar.")

            if any(it.producto.es_serializado and not it.seriales_seleccionados.exists()
                   for it in sal.items.select_related("producto")):
                messages.info(request, "Identifica los números de esta salida antes de confirmar el ingreso.")
                return redirect('conciliar_traspaso', sal_id=sal.pk)

            ing = DocumentoInventario.objects.create(
                tipo=TipoDocumento.ING,
                estado=EstadoDocumento.CONFIRMADO,
                sede=mi_sede,
                responsable=request.user,
                sede_origen=sal.sede,
                referencia=f"Ref: {sal.numero}",
                observaciones=f"Recepción automática de transferencia {sal.numero}",
                origen=sal,
                fecha=timezone.now()
            )
            ing.asignar_numero_si_falta()

            for item_sal in sal.items.all():
                linea_ing = DocumentoItem.objects.create(
                    documento=ing,
                    producto=item_sal.producto,
                    cantidad=item_sal.cantidad,
                    observacion=item_sal.observacion
                )

                mov = MovimientoInventario.objects.create(
                    producto=item_sal.producto,
                    sede=mi_sede,
                    ubicacion=None,
                    tipo=MovimientoInventario.TIPO_IN,
                    qty=item_sal.cantidad,
                    referencia=ing.numero,
                    usuario=request.user,
                    nota=f"Transferencia desde {sal.sede.nombre}"
                )
                mov.aplicar() 

                # Recepcionar exclusivamente los equipos documentados en ESTA salida.
                if item_sal.producto.es_serializado:
                    ubi_destino = Ubicacion.objects.filter(sede=mi_sede).first()
                    ids = list(item_sal.seriales_seleccionados.values_list("item_serializado_id", flat=True))
                    equipos = list(ItemSerializado.objects.select_for_update().filter(
                        pk__in=ids, producto=item_sal.producto,
                        asignado_a=sal.solicitante, sede_asignacion=sal.sede,
                        estado=ItemSerializado.Estado.ASIGNADO, proyecto__isnull=True,
                    ).order_by("id"))
                    if not ubi_destino or len(equipos) != item_sal.cantidad:
                        raise ValidationError("Concilia los seriales de esta salida antes de recibirla.")
                    for equipo in equipos:
                        equipo.estado = ItemSerializado.Estado.EN_ALMACEN
                        equipo.asignado_a = None
                        equipo.sede_asignacion = None
                        equipo.ubicacion = ubi_destino
                        equipo.save(update_fields=["estado", "asignado_a", "sede_asignacion", "ubicacion", "actualizado_en"])
                    registrar_seriales(linea_ing, equipos)

            sal.recibido = True
            sal.recibido_por = request.user
            sal.recibido_en = timezone.now()
            sal.save()

            messages.success(request, f"✅ Mercadería recepcionada correctamente con {ing.numero}. Stock actualizado.")

    except Exception as e:
        messages.error(request, f"Error al recepcionar: {str(e)}")

    return redirect('dash_almacen')
