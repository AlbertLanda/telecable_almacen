from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import ValidationError, PermissionDenied
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
        DocumentoInventario.objects.select_related("sede", "responsable", "origen"),
        id=sal_id,
        tipo=TipoDocumento.SAL,
    )
    items = sal.items.select_related("producto").order_by("producto__nombre")

    try:
        profile = getattr(request.user, "profile", None)
        if not profile:
            raise PermissionDenied("Usuario sin perfil (UserProfile).")

        # ✅ JEFA ve todo
        if profile.rol == UserProfile.Rol.JEFA:
            return render(request, "inventario/sal_detail.html", {"sal": sal, "items": items})

        # ✅ ADMIN: por ahora ve todo (si quieres lo restringimos luego)
        if profile.rol == UserProfile.Rol.ADMIN:
            return render(request, "inventario/sal_detail.html", {"sal": sal, "items": items})

        # ✅ ALMACÉN solo ve SAL de su sede
        if profile.rol == UserProfile.Rol.ALMACEN:
            sede = _sede_operativa(request.user)
            if sal.sede_id != sede.id:
                raise PermissionDenied("No puedes ver SAL de otra sede.")
            return render(request, "inventario/sal_detail.html", {"sal": sal, "items": items})

        # ✅ SOLICITANTE: solo si él es responsable o si la SAL viene de su REQ
        if profile.rol == UserProfile.Rol.SOLICITANTE:
            if sal.responsable_id == request.user.id:
                return render(request, "inventario/sal_detail.html", {"sal": sal, "items": items})

            if sal.origen_id and sal.origen and sal.origen.responsable_id == request.user.id:
                return render(request, "inventario/sal_detail.html", {"sal": sal, "items": items})

            raise PermissionDenied("No puedes ver una SAL que no es tuya.")

        raise PermissionDenied("Rol no autorizado.")

    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("/")


@require_POST
@login_required
def sal_confirmar(request, sal_id: int):
    sal = get_object_or_404(
        DocumentoInventario.objects.select_related("sede"),
        id=sal_id,
        tipo=TipoDocumento.SAL,
    )

    try:
        profile = _require_roles(request.user, UserProfile.Rol.ALMACEN, UserProfile.Rol.JEFA)

        # ✅ ALMACÉN solo confirma SAL de su sede
        if profile.rol != UserProfile.Rol.JEFA:
            sede = _sede_operativa(request.user)
            if sal.sede_id != sede.id:
                raise PermissionDenied("No puedes confirmar SAL de otra sede.")

        # ✅ Confirmar SAL (tu models.py soporta entregado_por)
        sal.confirmar(entregado_por=request.user)

        messages.success(request, f"SAL confirmada: {sal.numero}")

    except (ValidationError, PermissionDenied) as e:
        messages.error(request, str(e))
    except Exception as e:
        messages.error(request, f"Error al confirmar SAL: {e}")

    return redirect(f"/sal/{sal_id}/")
