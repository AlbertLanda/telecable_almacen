from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import models
from django.shortcuts import render, redirect

from inventario.models import DocumentoInventario, TipoDocumento, UserProfile


def _require_roles(user, *roles):
    profile = getattr(user, "profile", None)
    if not profile:
        raise PermissionDenied("Usuario sin perfil (UserProfile).")
    if profile.rol not in roles:
        raise PermissionDenied("No autorizado.")
    return profile


@login_required
def tecnico_dashboard(request):
    """
    Dashboard del técnico (SOLICITANTE) - pantalla principal.
    """
    try:
        _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("dashboard")  # ✅ fallback seguro

    return render(request, "inventario/tecnico_dashboard.html")


@login_required
def tecnico_mis_reqs(request):
    """
    Historial de REQs del técnico.
    """
    _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)

    reqs = (
        DocumentoInventario.objects.filter(
            tipo=TipoDocumento.REQ,
            responsable=request.user,
        )
        .order_by("-fecha")[:50]
    )

    return render(request, "inventario/tecnico_mis_reqs.html", {"reqs": reqs})


@login_required
def tecnico_mis_entregas(request):
    """
    Entregas = SAL relacionadas al técnico:
    - SAL donde él es responsable
    - o SAL cuyo origen (REQ) pertenece a él
    """
    _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)

    sals = (
        DocumentoInventario.objects.filter(tipo=TipoDocumento.SAL)
        .filter(
            models.Q(responsable=request.user)
            | models.Q(origen__tipo=TipoDocumento.REQ, origen__responsable=request.user)
        )
        .select_related("origen")
        .order_by("-fecha")[:50]
    )

    return render(request, "inventario/tecnico_mis_entregas.html", {"sals": sals})
