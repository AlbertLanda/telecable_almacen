from __future__ import annotations

from datetime import timedelta

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.exceptions import PermissionDenied
from django.db import models
from django.db.models import Count
from django.db.models.functions import TruncDate
from django.shortcuts import render, redirect
from django.utils import timezone

from inventario.models import DocumentoInventario, TipoDocumento, EstadoDocumento, UserProfile


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
    Render server-side (sin depender de /api/tecnico/dashboard/).
    """
    try:
        profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    except PermissionDenied as e:
        messages.error(request, str(e))
        return redirect("dashboard")  # fallback seguro

    sede = None
    try:
        sede = profile.get_sede_operativa()
    except Exception:
        sede = None

    # -------------------------
    # KPIs
    # -------------------------
    reqs_qs = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.REQ,
        responsable=request.user,
    )

    kpis = {
        "reqs_activos": reqs_qs.filter(
            estado__in=[EstadoDocumento.REQ_BORRADOR, EstadoDocumento.REQ_PENDIENTE]
        ).count(),
        "reqs_atendidos": reqs_qs.filter(estado=EstadoDocumento.REQ_ATENDIDO).count(),
        # Entregas: SAL confirmadas relacionadas al técnico
        "entregas": DocumentoInventario.objects.filter(
            tipo=TipoDocumento.SAL,
            estado=EstadoDocumento.CONFIRMADO,
        ).filter(
            models.Q(responsable=request.user)
            | models.Q(origen__tipo=TipoDocumento.REQ, origen__responsable=request.user)
        ).count(),
    }

    # -------------------------
    # Charts: últimos 7 días
    # -------------------------
    today = timezone.localdate()
    start_day = today - timedelta(days=6)  # 7 días incluyendo hoy

    # Conteo por día (REQ creados)
    per_day = (
        reqs_qs.filter(fecha__date__gte=start_day, fecha__date__lte=today)
        .annotate(d=TruncDate("fecha"))
        .values("d")
        .annotate(c=Count("id"))
        .order_by("d")
    )
    map_day = {row["d"]: row["c"] for row in per_day}

    req_labels = []
    req_data = []
    for i in range(7):
        d = start_day + timedelta(days=i)
        req_labels.append(d.strftime("%d/%m"))
        req_data.append(int(map_day.get(d, 0)))

    # Doughnut: estados (global del técnico, puedes limitar a 30 días si quieres)
    estados_count = (
        reqs_qs.values("estado")
        .annotate(c=Count("id"))
        .order_by("estado")
    )
    estado_labels = []
    estado_data = []
    for row in estados_count:
        estado = row["estado"]
        c = int(row["c"])
        # Labels “bonitos”
        if estado == EstadoDocumento.REQ_BORRADOR:
            estado_labels.append("Borrador")
        elif estado == EstadoDocumento.REQ_PENDIENTE:
            estado_labels.append("Pendiente")
        elif estado == EstadoDocumento.REQ_ATENDIDO:
            estado_labels.append("Atendido")
        else:
            estado_labels.append(str(estado))
        estado_data.append(c)

    chart = {
        "req_labels": req_labels,
        "req_data": req_data,
        "estado_labels": estado_labels,
        "estado_data": estado_data,
    }

    # -------------------------
    # Tabla: últimos REQs
    # -------------------------
    reqs_recientes = (
        reqs_qs.order_by("-fecha")
        .only("id", "numero", "fecha", "estado")
        [:10]
    )

    return render(
        request,
        "inventario/tecnico_dashboard.html",
        {
            "sede": sede,
            "kpis": kpis,
            "chart": chart,
            "reqs_recientes": reqs_recientes,
        },
    )


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
