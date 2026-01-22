from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib.auth.mixins import LoginRequiredMixin
from django.contrib import messages
from django.db.models import Sum, Count, Q, F
from django.http import JsonResponse, HttpResponse
from django.views.decorators.csrf import csrf_exempt, csrf_protect
from django.utils.decorators import method_decorator
from django.views.generic import ListView, DetailView
from django.utils import timezone
from django.core.paginator import Paginator
from datetime import datetime, timedelta
import json

from .models_liquidacion import LiquidacionSemanal, LiquidacionLog
from .models import Sede, Producto, Stock
from .services.liquidacion_service import LiquidacionService


def get_user_sede_info(user):
    """
    Obtener información de la sede del usuario y sus permisos
    Retorna: (sede_principal, es_sede_central, puede_liquidar_central)
    """
    if not hasattr(user, 'profile'):
        return None, False, False
    
    profile = user.profile
    sede = profile.sede_principal or profile.sede_activa
    
    if not sede:
        return None, False, False
    
    es_sede_central = sede.tipo == 'CENTRAL'
    
    # Puede liquidar central si es admin, jefa, o almacén de sede central
    puede_liquidar_central = profile.rol in ['ADMIN', 'JEFA'] or (profile.rol == 'ALMACEN' and es_sede_central)
    
    return sede, es_sede_central, puede_liquidar_central


def user_can_liquidar(user):
    """
    Verificar si el usuario tiene permisos para liquidar
    """
    if not user.is_authenticated:
        return False
    
    if hasattr(user, 'profile'):
        # Solo ALMACEN, ADMIN y JEFA pueden liquidar
        return user.profile.rol in ['ALMACEN', 'ADMIN', 'JEFA']
    
    return False


def user_can_liquidar_sede(user, sede_id):
    """
    Verificar si el usuario puede liquidar una sede específica
    """
    if not user_can_liquidar(user):
        return False
    
    profile = user.profile
    
    # ADMIN y JEFA pueden liquidar cualquier sede
    if profile.rol in ['ADMIN', 'JEFA']:
        return True
    
    # ALMACEN solo puede liquidar su sede (o todas si es de central)
    if profile.rol == 'ALMACEN':
        sede_usuario = profile.sede_principal or profile.sede_activa
        if sede_usuario:
            # Si es de sede central, puede liquidar cualquier sede
            if sede_usuario.tipo == 'CENTRAL':
                return True
            # Si es de sede secundaria, solo puede liquidar su propia sede
            return sede_usuario.id == sede_id
    
    return False


def puede_liquidar_hoy():
    """
    Verificar si hoy es un día permitido para liquidar (sábado, domingo o lunes)
    Retorna: (puede_liquidar, mensaje, dias_para_lunes)
    """
    hoy = timezone.now()
    dia_semana = hoy.weekday()  # 0 = Lunes, 6 = Domingo
    
    # Puede liquidar sábado (5), domingo (6) o lunes (0)
    if dia_semana in [5, 6, 0]:
        if dia_semana == 5:
            return True, "Hoy es sábado, puede realizar la liquidación.", 3
        elif dia_semana == 6:
            return True, "Hoy es domingo, puede realizar la liquidación.", 2
        else:
            return True, "Hoy es lunes, puede realizar la liquidación.", 0
    
    dias = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo']
    dias_para_lunes = (7 - dia_semana) % 7
    if dias_para_lunes == 0:
        dias_para_lunes = 7
    
    return False, f"Hoy es {dias[dia_semana]}. La liquidación está habilitada desde Sábado hasta Lunes. Faltan {dias_para_lunes} días para el sábado.", dias_para_lunes


def get_semana_a_liquidar():
    """
    Obtener la semana que se debe liquidar (semana anterior)
    """
    hoy = timezone.now()
    # La semana a liquidar es la anterior
    semana_anterior = hoy - timedelta(days=7)
    return semana_anterior.isocalendar()[1], semana_anterior.year


@login_required
def liquidacion_dashboard(request):
    """
    Dashboard principal de liquidación - muestra opciones según rol del usuario
    """
    # Verificar permisos básicos
    if not user_can_liquidar(request.user):
        messages.error(request, 'No tienes permisos para acceder a la liquidación.')
        return redirect('home')
    
    profile = request.user.profile
    sede_usuario, es_sede_central, puede_liquidar_central = get_user_sede_info(request.user)
    
    # Verificar si puede liquidar hoy
    puede_liquidar, mensaje_dia, dias_para_lunes = puede_liquidar_hoy()
    semana_liquidar, anio_liquidar = get_semana_a_liquidar()
    
    # Obtener sedes que el usuario puede liquidar
    if profile.rol in ['ADMIN', 'JEFA'] or es_sede_central:
        # Puede ver todas las sedes
        sedes_disponibles = Sede.objects.filter(activo=True).order_by('tipo', 'nombre')
    else:
        # Solo puede ver su sede
        sedes_disponibles = Sede.objects.filter(id=sede_usuario.id) if sede_usuario else Sede.objects.none()
    
    # Separar sedes por tipo
    sede_central = sedes_disponibles.filter(tipo='CENTRAL').first()
    sedes_secundarias = sedes_disponibles.filter(tipo='SECUNDARIO')
    
    # Obtener liquidaciones recientes
    service = LiquidacionService()
    
    # Filtrar liquidaciones según permisos
    if profile.rol in ['ADMIN', 'JEFA'] or es_sede_central:
        liquidaciones = LiquidacionSemanal.objects.all()
    else:
        liquidaciones = LiquidacionSemanal.objects.filter(sede=sede_usuario) if sede_usuario else LiquidacionSemanal.objects.none()
    
    liquidaciones = liquidaciones.select_related('sede', 'producto', 'liquidado_por').order_by('-fecha_liquidacion', '-id')[:50]
    
    # Obtener resumen
    hoy = timezone.now()
    semana_actual = hoy.isocalendar()[1]
    anio_actual = hoy.year
    
    # Verificar estado de liquidación de la semana anterior
    liquidaciones_semana = LiquidacionSemanal.objects.filter(
        semana=semana_liquidar,
        anio=anio_liquidar
    )
    
    # Estado por sede
    estado_sedes = []
    for sede in sedes_disponibles:
        liquidada = liquidaciones_semana.filter(sede=sede).exists()
        estado_sedes.append({
            'sede': sede,
            'liquidada': liquidada,
            'puede_liquidar': user_can_liquidar_sede(request.user, sede.id)
        })
    
    # Preparar datos de permisos para cada sede
    user_permissions = {}
    for sede in sedes_disponibles:
        user_permissions[sede.id] = user_can_liquidar_sede(request.user, sede.id)
    
    context = {
        'liquidaciones': liquidaciones,
        'sedes_disponibles': sedes_disponibles,
        'sede_central': sede_central,
        'sedes_secundarias': sedes_secundarias,
        'sede_usuario': sede_usuario,
        'es_sede_central': es_sede_central,
        'puede_liquidar_central': puede_liquidar_central,
        'puede_liquidar': puede_liquidar,
        'mensaje_dia': mensaje_dia,
        'dias_para_lunes': dias_para_lunes,
        'semana_liquidar': semana_liquidar,
        'anio_liquidar': anio_liquidar,
        'semana_actual': semana_actual,
        'anio_actual': anio_actual,
        'estado_sedes': estado_sedes,
        'rol_usuario': profile.rol,
        'user_can_liquidar_sede': user_permissions,
        'liquidaciones_pendientes': len([s for s in sedes_disponibles if s.id not in [estado_sede['sede'].id for estado_sede in estado_sedes if estado_sede['liquidada']]]),
    }
    
    return render(request, 'inventario/liquidacion/dashboard_dark.html', context)


@login_required
@csrf_protect
def liquidar_sede(request, sede_id):
    """
    Vista para liquidar una sede específica
    """
    # Verificar permisos
    if not user_can_liquidar_sede(request.user, sede_id):
        messages.error(request, 'No tienes permisos para liquidar esta sede.')
        return redirect('liquidacion_dashboard')
    
    sede = get_object_or_404(Sede, id=sede_id)
    puede_liquidar, mensaje_dia, dias_para_lunes = puede_liquidar_hoy()
    semana_liquidar, anio_liquidar = get_semana_a_liquidar()
    
    if request.method == 'POST':
        # Verificar si es día permitido
        if not puede_liquidar:
            messages.error(request, mensaje_dia)
            return redirect('liquidacion_dashboard')
        
        semana = int(request.POST.get('semana', semana_liquidar))
        anio = int(request.POST.get('anio', anio_liquidar))
        observaciones = request.POST.get('observaciones', '')
        
        service = LiquidacionService()
        
        try:
            resultado = service.liquidar_sede(
                sede_id=sede_id,
                semana=semana,
                anio=anio,
                usuario_id=request.user.id,
                observaciones=observaciones
            )
            
            if resultado:
                messages.success(request, f'Liquidación de {sede.nombre} completada. {len(resultado)} productos procesados.')
                
                # Verificar discrepancias
                discrepancias = [r for r in resultado if r.get('diferencia', 0) != 0]
                if discrepancias:
                    messages.warning(request, f'Se detectaron {len(discrepancias)} productos con discrepancias.')
            else:
                messages.info(request, 'No hay productos para liquidar en esta sede.')
                
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')
        
        return redirect('liquidacion_dashboard')
    
    # GET - mostrar formulario
    # Obtener stock actual de la sede
    stock_sede = Stock.objects.filter(sede=sede, cantidad__gt=0).select_related('producto')
    
    context = {
        'sede': sede,
        'puede_liquidar': puede_liquidar,
        'mensaje_dia': mensaje_dia,
        'semana_liquidar': semana_liquidar,
        'anio_liquidar': anio_liquidar,
        'stock_sede': stock_sede,
    }
    
    return render(request, 'inventario/liquidacion/liquidar_sede.html', context)


@login_required
@csrf_protect
def liquidar_central(request):
    """
    Vista para liquidar almacén central (verificación de consistencia global)
    """
    sede_usuario, es_sede_central, puede_liquidar_central = get_user_sede_info(request.user)
    
    # Verificar permisos
    if not puede_liquidar_central:
        messages.error(request, 'No tienes permisos para realizar la liquidación central. Solo usuarios de sede central, administradores o jefas pueden hacerlo.')
        return redirect('liquidacion_dashboard')
    
    puede_liquidar, mensaje_dia, dias_para_lunes = puede_liquidar_hoy()
    semana_liquidar, anio_liquidar = get_semana_a_liquidar()
    
    if request.method == 'POST':
        # Verificar si es día permitido
        if not puede_liquidar:
            messages.error(request, mensaje_dia)
            return redirect('liquidacion_dashboard')
        
        semana = int(request.POST.get('semana', semana_liquidar))
        anio = int(request.POST.get('anio', anio_liquidar))
        observaciones = request.POST.get('observaciones', '')
        
        service = LiquidacionService()
        
        try:
            resultado = service.liquidar_central(
                semana=semana,
                anio=anio,
                usuario_id=request.user.id,
                observaciones=observaciones
            )
            
            if resultado:
                messages.success(request, f'Liquidación central completada. {len(resultado)} productos procesados.')
                
                # Verificar inconsistencias
                inconsistentes = [r for r in resultado if r.get('consistencia_global') == 'INCONSISTENTE']
                if inconsistentes:
                    messages.warning(request, f'Se detectaron {len(inconsistentes)} productos inconsistentes.')
            else:
                messages.info(request, 'No hay productos para liquidar en el almacén central.')
                
        except Exception as e:
            messages.error(request, f'Error: {str(e)}')
        
        return redirect('liquidacion_dashboard')
    
    # GET - mostrar formulario
    # Obtener sedes activas para mostrar estado
    sedes_activas = Sede.objects.filter(activo=True).order_by('tipo', 'nombre')
    sede_central = sedes_activas.filter(tipo='CENTRAL').first()
    
    # Stock del almacén central
    stock_central = Stock.objects.filter(sede=sede_central, cantidad__gt=0).select_related('producto') if sede_central else []
    
    context = {
        'puede_liquidar': puede_liquidar,
        'mensaje_dia': mensaje_dia,
        'semana_liquidar': semana_liquidar,
        'anio_liquidar': anio_liquidar,
        'sedes_activas': sedes_activas,
        'sede_central': sede_central,
        'stock_central': stock_central,
    }
    
    return render(request, 'inventario/liquidacion/liquidar_central.html', context)


@login_required
def liquidacion_detalle(request, liquidacion_id):
    """
    Vista de detalle de una liquidación específica
    """
    liquidacion = LiquidacionSemanal.objects.get(id=liquidacion_id)
    
    # Obtener movimientos relacionados de la semana
    fecha_inicio = timezone.datetime.strptime(f"{liquidacion.anio}-{liquidacion.semana}-1", "%Y-%W-%w")
    fecha_fin = fecha_inicio + timedelta(days=6)
    
    context = {
        'liquidacion': liquidacion,
        'fecha_inicio': fecha_inicio,
        'fecha_fin': fecha_fin,
    }
    
    return render(request, 'inventario/liquidacion/detalle.html', context)


@login_required
def liquidacion_api_resumen(request):
    """
    API para obtener resumen de liquidación (para dashboard)
    """
    semana = request.GET.get('semana')
    anio = request.GET.get('anio')
    sede_id = request.GET.get('sede_id')
    
    service = LiquidacionService()
    
    try:
        resumen = service.obtener_resumen(
            semana=int(semana) if semana else None,
            anio=int(anio) if anio else None,
            sede_id=int(sede_id) if sede_id else None
        )
        
        return JsonResponse({
            'success': True,
            'data': resumen
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        })

@login_required
def liquidacion_api_graficos(request):
    """
    API para obtener datos para gráficos
    """
    filtro_tipo = request.GET.get('filtro_tipo', '')
    filtro_valor = request.GET.get('filtro_valor', '')
    
    service = LiquidacionService()
    
    try:
        # Datos para gráfico de stock
        datos_stock = service.obtener_datos_grafico_stock(
            filtro_tipo=filtro_tipo if filtro_tipo else None,
            filtro_valor=int(filtro_valor) if filtro_valor else None
        )
        
        # Datos para gráfico de sedes
        datos_sedes = service.obtener_datos_grafico_sedes(
            filtro_tipo=filtro_tipo if filtro_tipo else None,
            filtro_valor=int(filtro_valor) if filtro_valor else None
        )
        
        return JsonResponse({
            'success': True,
            'stock': datos_stock,
            'sedes': datos_sedes
        })
    except Exception as e:
        return JsonResponse({
            'success': False,
            'error': str(e)
        })


@login_required
def liquidacion_exportar_excel(request):
    """
    Exportar liquidaciones a Excel
    """
    filtro_tipo = request.GET.get('filtro_tipo', '')
    filtro_valor = request.GET.get('filtro_valor', '')
    sede_id = request.GET.get('sede_id', '')
    producto_id = request.GET.get('producto_id', '')
    
    service = LiquidacionService()
    
    try:
        # Obtener datos
        liquidaciones = service.obtener_liquidaciones_filtradas(
            filtro_tipo=filtro_tipo if filtro_tipo else None,
            filtro_valor=int(filtro_valor) if filtro_valor else None,
            sede_id=int(sede_id) if sede_id else None,
            producto_id=int(producto_id) if producto_id else None
        )
        
        # Generar Excel
        response = service.exportar_excel(liquidaciones)
        
        return response
        
    except Exception as e:
        messages.error(request, f'Error al exportar: {str(e)}')
        return redirect('liquidacion_dashboard')


class LiquidacionListView(LoginRequiredMixin, ListView):
    """
    Vista de lista de liquidaciones con paginación
    """
    model = LiquidacionSemanal
    template_name = 'inventario/liquidacion/lista.html'
    context_object_name = 'liquidaciones'
    paginate_by = 50
    
    def get_queryset(self):
        queryset = super().get_queryset()
        
        # Verificar permisos
        if not user_can_liquidar(self.request.user):
            return queryset.none()
        
        profile = self.request.user.profile
        sede_usuario, es_sede_central, puede_liquidar_central = get_user_sede_info(self.request.user)
        
        # Filtrar según permisos
        if profile.rol in ['ADMIN', 'JEFA'] or es_sede_central:
            # Puede ver todas las liquidaciones
            pass
        else:
            # Solo puede ver liquidaciones de su sede
            if sede_usuario:
                queryset = queryset.filter(sede=sede_usuario)
            else:
                queryset = queryset.none()
        
        # Aplicar filtros del formulario
        sede = self.request.GET.get('sede', '')
        semana = self.request.GET.get('semana', '')
        anio = self.request.GET.get('anio', '')
        
        if sede:
            queryset = queryset.filter(sede_id=sede)
        if semana:
            queryset = queryset.filter(semana=semana)
        if anio:
            queryset = queryset.filter(anio=anio)
        
        return queryset.select_related('sede', 'liquidado_por').order_by('-creado_en')
    
    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        
        # Agregar datos para filtros y contexto
        profile = self.request.user.profile
        sede_usuario, es_sede_central, puede_liquidar_central = get_user_sede_info(self.request.user)
        
        # Sedes disponibles para filtros
        if profile.rol in ['ADMIN', 'JEFA'] or es_sede_central:
            sedes_disponibles = Sede.objects.filter(activo=True).order_by('tipo', 'nombre')
        else:
            sedes_disponibles = Sede.objects.filter(id=sede_usuario.id) if sede_usuario else Sede.objects.none()
        
        context.update({
            'sedes_disponibles': sedes_disponibles,
            'user_info': {
                'rol': profile.rol,
                'sede_usuario': sede_usuario,
                'es_sede_central': es_sede_central,
                'puede_liquidar_central': puede_liquidar_central,
            },
            'anio_actual': timezone.now().year,
        })
        
        return context
