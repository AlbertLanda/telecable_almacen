from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST
from django.utils import timezone
from inventario.permissions import role_required, sede_required, get_profile

from inventario.models import (
    DocumentoInventario, 
    TipoDocumento, 
    UserProfile, 
    EstadoDocumento, 
    MovimientoInventario, 
    DocumentoItem
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
        DocumentoInventario.objects.select_related("sede", "responsable", "origen", "ubicacion", "sede_destino"),
        id=sal_id,
        tipo=TipoDocumento.SAL,
    )
    items = sal.items.select_related("producto").order_by("producto__nombre")

    try:
        profile = get_profile(request.user)
        if not profile:
            raise PermissionDenied("Usuario sin perfil.")

        # Reglas de visualización
        allowed = False
        
        # 1. Admin/Jefa ven todo
        if profile.rol in (UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN):
            allowed = True
            
        # 2. Almacén ve si es SU sede de origen O SU sede de destino
        elif profile.rol == UserProfile.Rol.ALMACEN:
            sede_user = _sede_operativa(request.user)
            
            # Caso A: Yo la envié (Soy Jauja)
            if sal.sede_id == sede_user.id: 
                allowed = True
            # Caso B: Me la enviaron a mí (Soy Oroya)
            elif sal.sede_destino_id == sede_user.id: 
                allowed = True
                
        # 3. Técnico ve si es suyo (Solo para SAL locales)
        elif profile.rol == UserProfile.Rol.SOLICITANTE:
            allowed = (
                sal.responsable_id == request.user.id
                or (sal.origen_id and sal.origen and sal.origen.responsable_id == request.user.id)
            )

        if not allowed:
            raise PermissionDenied("No tienes permisos para ver esta Guía de Salida.")

        return render(request, "inventario/sal_detail.html", {"sal": sal, "items": items})

    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/")


@require_POST
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN)
@transaction.atomic
def sal_confirmar(request, sal_id: int):
    """
    Confirma una SAL (Manual): descuenta stock y crea movimientos.
    Solo Almacén, Jefa o Admin.
    """
    try:
        profile = request.user_profile

        sal = DocumentoInventario.objects.select_for_update().get(
            id=sal_id, tipo=TipoDocumento.SAL
        )

        # Solo JEFA puede confirmar otras sedes
        if profile.rol not in (UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN):
            sede = _sede_operativa(request.user)
            if sal.sede_id != sede.id:
                raise PermissionDenied("No puedes confirmar SAL de otra sede.")

        if sal.estado == EstadoDocumento.CONFIRMADO:
            messages.info(request, f"Esta SAL ya estaba confirmada: {sal.numero}")
            return redirect(f"/sal/{sal_id}/")

        sal.confirmar(entregado_por=request.user)
        messages.success(request, f"SAL confirmada: {sal.numero}")

    except DocumentoInventario.DoesNotExist:
        messages.error(request, "SAL no encontrada.")
    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
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

    # Validaciones de permiso igual que detail (Simplificado para impresión)
    profile = get_profile(request.user)
    if not profile:
        messages.error(request, "Usuario sin perfil.")
        return redirect("/")
    if profile.rol == UserProfile.Rol.SOLICITANTE:
        if sal.responsable_id != request.user.id:
             # Si no es responsable directo, check si es origen
             if not (sal.origen and sal.origen.responsable_id == request.user.id):
                 messages.error(request, "No autorizado.")
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
    """
    Recibe una SAL de otra sede y genera automáticamente el ING en mi sede.
    """
    try:
        with transaction.atomic():
            # 1. Buscamos la SAL que viene hacia mí
            # Usamos el helper _sede_operativa para mayor seguridad
            mi_sede = _sede_operativa(request.user)
            
            sal = get_object_or_404(DocumentoInventario, id=sal_id, tipo=TipoDocumento.SAL)

            if sal.sede_destino != mi_sede:
                raise PermissionDenied("Este traspaso no es para tu sede.")
            
            if sal.recibido:
                messages.warning(request, "Este traspaso ya fue recepcionado.")
                return redirect('dash_almacen')

            # 2. Crear el documento de INGRESO (ING) en mi sede
            ing = DocumentoInventario.objects.create(
                tipo=TipoDocumento.ING,
                estado=EstadoDocumento.CONFIRMADO, # Confirmado directo
                sede=mi_sede,                      # Entra a MI sede
                responsable=request.user,          # Yo lo recibo
                sede_origen=sal.sede,              # Viene de allá
                referencia=f"Ref: {sal.numero}",   # Referencia a la SAL original
                observaciones=f"Recepción automática de transferencia {sal.numero}",
                origen=sal,
                fecha=timezone.now()
            )
            ing.asignar_numero_si_falta()

            # 3. Copiar items y mover stock (SUMAR a mi inventario)
            for item_sal in sal.items.all():
                # A. Crear Item en el ING
                DocumentoItem.objects.create(
                    documento=ing,
                    producto=item_sal.producto,
                    cantidad=item_sal.cantidad,
                    observacion=item_sal.observacion
                )

                # B. Movimiento Físico (SUMAR STOCK)
                mov = MovimientoInventario.objects.create(
                    producto=item_sal.producto,
                    sede=mi_sede,
                    ubicacion=None, # O una ubicación de recepción por defecto
                    tipo=MovimientoInventario.TIPO_IN, # Entrada
                    qty=item_sal.cantidad,
                    referencia=ing.numero,
                    usuario=request.user,
                    nota=f"Transferencia desde {sal.sede.nombre}"
                )
                mov.aplicar() # ¡Aquí sube tu stock!

            # 4. Marcar la SAL original como RECIBIDA
            sal.recibido = True
            sal.recibido_por = request.user
            sal.recibido_en = timezone.now()
            sal.save()

            messages.success(request, f"✅ Mercadería recepcionada correctamente con {ing.numero}. Stock actualizado.")

    except Exception as e:
        messages.error(request, f"Error al recepcionar: {str(e)}")

    return redirect('dash_almacen')