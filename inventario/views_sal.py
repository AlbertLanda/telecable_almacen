from __future__ import annotations

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
from django.db import transaction
from django.shortcuts import render, get_object_or_404, redirect
from django.views.decorators.http import require_POST

from inventario.models import DocumentoInventario, TipoDocumento, UserProfile


def _require_roles(user, *roles):
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No tienes permisos para esta acción.")
    return profile


def _sede_operativa(user):
    profile = getattr(user, "profile", None)
    if not profile:
        raise ValidationError("Usuario sin perfil (UserProfile).")
    sede = profile.get_sede_operativa()
    if not sede:
        raise ValidationError("No tienes sede operativa asignada.")
    return sede


@login_required
def sal_detail(request, sal_id: int):
    sal = get_object_or_404(
        DocumentoInventario.objects.select_related("sede", "responsable", "origen", "ubicacion"),
        id=sal_id,
        tipo=TipoDocumento.SAL,
    )
    items = sal.items.select_related("producto").order_by("producto__nombre")

    try:
        profile = getattr(request.user, "profile", None)
        if not profile:
            raise PermissionDenied("Usuario sin perfil (UserProfile).")

        # JEFA / ADMIN ven todo
        if profile.rol in (UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN):
            allowed = True

        # ALMACEN: solo su sede
        elif profile.rol == UserProfile.Rol.ALMACEN:
            sede = _sede_operativa(request.user)
            allowed = (sal.sede_id == sede.id)

        # SOLICITANTE: si es responsable o si la SAL viene de su REQ
        elif profile.rol == UserProfile.Rol.SOLICITANTE:
            allowed = (
                sal.responsable_id == request.user.id
                or (sal.origen_id and sal.origen and sal.origen.responsable_id == request.user.id)
            )

        else:
            allowed = False

        if not allowed:
            raise PermissionDenied("No puedes ver esta SAL.")

        return render(request, "inventario/sal_detail.html", {"sal": sal, "items": items})

    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/")


@require_POST
@login_required
@transaction.atomic
def sal_confirmar(request, sal_id: int):
    """
    Confirma una SAL:
    - bloquea la SAL (sin joins) para evitar problemas de DB
    - llama sal.confirmar() que descuenta stock y crea movimientos
    """
    try:
        profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)

        # 🔒 Bloqueo sin joins
        sal = DocumentoInventario.objects.select_for_update().get(id=sal_id, tipo=TipoDocumento.SAL)

        # ALMACEN solo confirma SAL de su sede
        if profile.rol != UserProfile.Rol.JEFA:
            sede = _sede_operativa(request.user)
            if sal.sede_id != sede.id:
                raise PermissionDenied("No puedes confirmar SAL de otra sede.")

        # ✅ Si ya está confirmada
        if sal.estado == "CONFIRMADO":  # <- ideal: usar constante EstadoDocumento.SAL_CONFIRMADO
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

    try:
        profile = getattr(request.user, "profile", None)
        if not profile:
            raise PermissionDenied("Usuario sin perfil (UserProfile).")

        # ✅ JEFA / ADMIN ven todo
        if profile.rol in (UserProfile.Rol.JEFA, UserProfile.Rol.ADMIN):
            return render(request, "inventario/sal_print.html", {
                "sal": sal,
                "items": items,
                "total_cantidad": total_cantidad,
            })

        # ✅ ALMACEN: solo su sede
        if profile.rol == UserProfile.Rol.ALMACEN:
            sede = _sede_operativa(request.user)
            if sal.sede_id != sede.id:
                raise PermissionDenied("No puedes ver SAL de otra sede.")
            return render(request, "inventario/sal_print.html", {
                "sal": sal,
                "items": items,
                "total_cantidad": total_cantidad,
            })

        # ✅ SOLICITANTE: solo si es suyo o viene de su REQ
        if profile.rol == UserProfile.Rol.SOLICITANTE:
            if sal.responsable_id == request.user.id:
                return render(request, "inventario/sal_print.html", {
                    "sal": sal,
                    "items": items,
                    "total_cantidad": total_cantidad,
                })

            if sal.origen_id and sal.origen and sal.origen.responsable_id == request.user.id:
                return render(request, "inventario/sal_print.html", {
                    "sal": sal,
                    "items": items,
                    "total_cantidad": total_cantidad,
                })

            raise PermissionDenied("No puedes ver una SAL que no es tuya.")

        raise PermissionDenied("Rol no autorizado.")

    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/")