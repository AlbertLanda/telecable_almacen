from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.db import models
from django.db.models import Sum, Count, Q, F
from django.db.models.functions import TruncDate
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_protect
from django.views.generic import ListView
from django.utils import timezone
from django.core.exceptions import PermissionDenied
from datetime import timedelta

# Importamos modelos del Core
from inventario.models import Sede, Producto, Stock, UserProfile, DocumentoInventario, TipoDocumento, EstadoDocumento
# Importamos modelos de esta app
from operaciones.models import LiquidacionSemanal, LiquidacionLog
# Importamos servicios
from operaciones.services import LiquidacionService

# ========================================================
# HELPERS
# ========================================================

def get_user_sede_info(user):
    if not hasattr(user, 'profile'): return None, False, False
    profile = user.profile
    sede = profile.sede_principal or profile.sede_activa
    if not sede: return None, False, False
    es_sede_central = (sede.tipo == 'CENTRAL')
    puede_liquidar_central = profile.rol in ['ADMIN', 'JEFA'] or (profile.rol == 'ALMACEN' and es_sede_central)
    return sede, es_sede_central, puede_liquidar_central

def user_can_liquidar(user):
    if not user.is_authenticated: return False
    if hasattr(user, 'profile'): return user.profile.rol in ['ALMACEN', 'ADMIN', 'JEFA']
    return False

def user_can_liquidar_sede(user, sede_id):
    if not user_can_liquidar(user): return False
    profile = user.profile
    if profile.rol in ['ADMIN', 'JEFA']: return True
    if profile.rol == 'ALMACEN':
        sede_usuario = profile.sede_principal or profile.sede_activa
        if sede_usuario:
            if sede_usuario.tipo == 'CENTRAL': return True
            return sede_usuario.id == sede_id
    return False

def puede_liquidar_hoy():
    hoy = timezone.now()
    dia_semana = hoy.weekday()
    if dia_semana in [5, 6, 0]: # Sáb, Dom, Lun
        return True, "Hoy es día de liquidación habilitado.", (7 - dia_semana) % 7 if dia_semana != 0 else 0
    dias_para_lunes = (7 - dia_semana) % 7
    return False, "La liquidación solo está habilitada Sábado, Domingo y Lunes.", dias_para_lunes

def get_semana_a_liquidar():
    hoy = timezone.now()
    semana_anterior = hoy - timedelta(days=7)
    return semana_anterior.isocalendar()[1], semana_anterior.year

def _require_roles(user, *roles):
    profile = getattr(user, "profile", None)
    if not profile: raise PermissionDenied("Usuario sin perfil.")
    if profile.rol not in roles: raise PermissionDenied("No autorizado.")
    return profile

# ========================================================
# VISTAS DE LIQUIDACIÓN (ALMACÉN)
# ========================================================

@login_required
def liquidacion_dashboard(request):
    if not user_can_liquidar(request.user):
        messages.error(request, 'No tienes permisos.')
        return redirect('home')
    
    profile = request.user.profile
    sede_usuario, es_sede_central, puede_liquidar_central = get_user_sede_info(request.user)
    puede_liquidar, mensaje_dia, dias_para_lunes = puede_liquidar_hoy()
    semana_liquidar, anio_liquidar = get_semana_a_liquidar()
    
    if profile.rol in ['ADMIN', 'JEFA'] or es_sede_central:
        sedes_disponibles = Sede.objects.filter(activo=True).order_by('tipo', 'nombre')
        liquidaciones = LiquidacionSemanal.objects.all().order_by('-fecha_liquidacion')[:50]
    else:
        sedes_disponibles = Sede.objects.filter(id=sede_usuario.id) if sede_usuario else Sede.objects.none()
        liquidaciones = LiquidacionSemanal.objects.filter(sede=sede_usuario).order_by('-fecha_liquidacion')[:50]
    
    liquidaciones_semana = LiquidacionSemanal.objects.filter(semana=semana_liquidar, anio=anio_liquidar)
    estado_sedes = []
    for sede in sedes_disponibles:
        estado_sedes.append({
            'sede': sede,
            'liquidada': liquidaciones_semana.filter(sede=sede).exists(),
            'puede_liquidar': user_can_liquidar_sede(request.user, sede.id)
        })
    
    context = {
        'liquidaciones': liquidaciones,
        'sedes_disponibles': sedes_disponibles,
        'sede_usuario': sede_usuario,
        'es_sede_central': es_sede_central,
        'puede_liquidar_central': puede_liquidar_central,
        'puede_liquidar': puede_liquidar,
        'mensaje_dia': mensaje_dia,
        'semana_liquidar': semana_liquidar,
        'anio_liquidar': anio_liquidar,
        'estado_sedes': estado_sedes,
        'rol_usuario': profile.rol,
    }
    return render(request, 'operaciones/liquidacion/dashboard_dark.html', context)

@login_required
@csrf_protect
def liquidar_sede(request, sede_id):
    if not user_can_liquidar_sede(request.user, sede_id):
        return redirect('liquidacion_dashboard')
    
    sede = get_object_or_404(Sede, id=sede_id)
    puede_liquidar, mensaje, _ = puede_liquidar_hoy()
    semana, anio = get_semana_a_liquidar()
    
    if request.method == 'POST':
        if not puede_liquidar:
            messages.error(request, mensaje)
            return redirect('liquidacion_dashboard')
        
        service = LiquidacionService()
        try:
            res = service.liquidar_sede(sede_id, semana, anio, request.user.id, request.POST.get('observaciones', ''))
            if res: messages.success(request, f'Liquidación completada. {len(res)} items procesados.')
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')
        return redirect('liquidacion_dashboard')
    
    stock_sede = Stock.objects.filter(sede=sede, cantidad__gt=0).select_related('producto')
    return render(request, 'operaciones/liquidacion/liquidar_sede.html', {
        'sede': sede, 'puede_liquidar': puede_liquidar, 'semana_liquidar': semana, 'anio_liquidar': anio, 'stock_sede': stock_sede
    })

@login_required
@csrf_protect
def liquidar_central(request):
    _, _, puede = get_user_sede_info(request.user)
    if not puede: return redirect('liquidacion_dashboard')
    
    puede_liq, mensaje, _ = puede_liquidar_hoy()
    semana, anio = get_semana_a_liquidar()
    
    if request.method == 'POST':
        if not puede_liq:
            messages.error(request, mensaje)
            return redirect('liquidacion_dashboard')
        try:
            LiquidacionService().liquidar_central(semana, anio, request.user.id, request.POST.get('observaciones'))
            messages.success(request, 'Liquidación central completada.')
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')
        return redirect('liquidacion_dashboard')
    
    return render(request, 'operaciones/liquidacion/liquidar_central.html', {
        'puede_liquidar': puede_liq, 'semana_liquidar': semana, 'anio_liquidar': anio
    })

@login_required
def liquidacion_detalle(request, liquidacion_id):
    liq = get_object_or_404(LiquidacionSemanal, id=liquidacion_id)
    return render(request, 'operaciones/liquidacion/detalle.html', {'liquidacion': liq})

@login_required
def liquidacion_api_resumen(request):
    return JsonResponse({'success': True}) 

@login_required
def liquidacion_api_graficos(request):
    return JsonResponse({'success': True})

@login_required
def liquidacion_exportar_excel(request):
    return redirect('liquidacion_dashboard')

class LiquidacionListView(LoginRequiredMixin, ListView):
    model = LiquidacionSemanal
    template_name = 'operaciones/liquidacion/lista.html' 
    context_object_name = 'liquidaciones'
    paginate_by = 50

# ========================================================
# VISTAS DEL TÉCNICO (Operativas)
# ========================================================

@login_required
def tecnico_dashboard(request):
    """
    Dashboard del técnico con KPIs, Gráfica Lineal y Gráfica Circular.
    """
    profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    sede = profile.get_sede_operativa()
    
    # Base Query: Todos los REQ de este usuario
    reqs_qs = DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ, responsable=request.user)
    
    # 1. KPIs
    kpis = {
        "reqs_activos": reqs_qs.filter(estado__in=[EstadoDocumento.REQ_BORRADOR, EstadoDocumento.REQ_PENDIENTE]).count(),
        "reqs_atendidos": reqs_qs.filter(estado=EstadoDocumento.REQ_ATENDIDO).count(),
        "entregas": DocumentoInventario.objects.filter(
            tipo=TipoDocumento.SAL, 
            estado=EstadoDocumento.CONFIRMADO
        ).filter(
            models.Q(responsable=request.user) | models.Q(origen__responsable=request.user)
        ).count(),
    }

    # 2. Datos para GRÁFICO LINEAL (Últimos 7 días)
    hoy = timezone.localdate()
    inicio_grafico = hoy - timedelta(days=6)
    
    datos_reqs = (
        reqs_qs
        .filter(fecha__date__gte=inicio_grafico, fecha__date__lte=hoy)
        .annotate(fecha_dia=TruncDate('fecha'))
        .values('fecha_dia')
        .annotate(cantidad=Count('id'))
        .order_by('fecha_dia')
    )
    mapa_reqs = {d['fecha_dia']: d['cantidad'] for d in datos_reqs}
    
    labels_linea = []
    data_linea = []
    for i in range(7):
        dia = inicio_grafico + timedelta(days=i)
        labels_linea.append(dia.strftime("%d/%m"))
        data_linea.append(mapa_reqs.get(dia, 0))

    # 3. Datos para GRÁFICO CIRCULAR (Estados) <--- ¡ESTO FALTABA!
    # Agrupamos por estado y contamos cuántos hay de cada uno
    estados_raw = reqs_qs.values('estado').annotate(total=Count('id'))
    
    labels_circulo = []
    data_circulo = []
    
    for e in estados_raw:
        # Convertimos el código "REQ_PENDIENTE" a texto legible
        nombre_estado = dict(EstadoDocumento.choices).get(e['estado'], e['estado'])
        # Opcional: Limpiar el texto para que se vea mejor en la gráfica
        nombre_estado = nombre_estado.replace("REQ - ", "") 
        
        labels_circulo.append(nombre_estado)
        data_circulo.append(e['total'])

    # 4. Empaquetar todo para el Template
    chart = {
        # Gráfica Lineal
        "req_labels": labels_linea,
        "req_data": data_linea,
        
        # Gráfica Circular (NUEVO)
        "estado_labels": labels_circulo,
        "estado_data": data_circulo,
    }

    reqs_recientes = reqs_qs.order_by("-fecha")[:10]
    
    return render(request, "operaciones/tecnico_dashboard.html", {
        "sede": sede, 
        "kpis": kpis, 
        "chart": chart,
        "reqs_recientes": reqs_recientes
    })

@login_required
def tecnico_mis_entregas(request):
    """
    Lista de entregas (SAL) recibidas por el técnico.
    """
    _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    
    # Buscamos SAL Confirmadas
    # Filtro: (Responsable es usuario) O (Origen REQ Responsable es usuario)
    sals = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.SAL,
        estado=EstadoDocumento.CONFIRMADO  # Solo mostramos confirmadas como "Entregas"
    ).filter(
        models.Q(responsable=request.user) |
        models.Q(origen__responsable=request.user)
    ).select_related("origen", "sede").order_by("-fecha")[:50]

    return render(request, "operaciones/tecnico_mis_entregas.html", {"sals": sals})

@login_required
def tecnico_mis_reqs(request):
    _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    reqs = DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ, responsable=request.user).order_by("-fecha")[:50]
    return render(request, "operaciones/tecnico_mis_reqs.html", {"reqs": reqs})