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
from django.db import transaction
from inventario.models import StockTecnico, DocumentoItem, MovimientoInventario
from django.contrib.auth import get_user_model

# Importamos modelos del Core
from inventario.models import Sede, Producto, Stock, UserProfile, DocumentoInventario, TipoDocumento, EstadoDocumento
# Importamos modelos de esta app
from operaciones.models import LiquidacionSemanal, LiquidacionLog
# Importamos servicios
from operaciones.services import LiquidacionService

User = get_user_model()

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

@login_required
def liquidacion_tecnico_lista(request):
    """
    Lista de técnicos que tienen material en su poder (Mochila > 0).
    FILTRO: Solo muestra técnicos de la misma SEDE que el usuario logueado.
    """
    # Validación de rol (Almacén, Admin, Jefa)
    if not request.user.profile.rol in ['ALMACEN', 'ADMIN', 'JEFA']:
        return redirect('home')
    
    # 1. Obtener la sede del almacenero logueado
    try:
        sede_actual = request.user.profile.get_sede_operativa()
    except:
        messages.error(request, "No tienes una sede asignada para ver liquidaciones.")
        return redirect('dash_almacen')

    # 2. Consulta filtrada por deuda y POR SEDE
    tecnicos_con_deuda = User.objects.filter(
        mi_stock__cantidad__gt=0,            # Que deba algo
        profile__sede_principal=sede_actual  # Que sea de MI sede
    ).distinct()
    
    return render(request, 'operaciones/liquidacion_tecnicos_lista.html', {
        'tecnicos': tecnicos_con_deuda
    })


@login_required
def liquidar_tecnico(request, tecnico_id):
    if not request.user.profile.rol in ['ALMACEN', 'ADMIN', 'JEFA']:
        return redirect('home')

    tecnico = get_object_or_404(User, id=tecnico_id)
    mochila = StockTecnico.objects.filter(tecnico=tecnico, cantidad__gt=0).select_related('producto')

    if request.method == 'POST':
        try:
            with transaction.atomic():
                sede_almacen = request.user.profile.get_sede_operativa()
                notas_usuario = request.POST.get('observaciones', '')
                
                # 1. Crear Cabecera
                doc_ing = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.ING,
                    estado=EstadoDocumento.CONFIRMADO, # Lo creamos ya confirmado
                    sede=sede_almacen,
                    responsable=request.user,
                    solicitante=tecnico,
                    referencia="LIQ-SEMANAL",
                    observaciones=f"Liquidación Semana. {notas_usuario}",
                    fecha=timezone.now()
                )
                doc_ing.asignar_numero_si_falta()
                
                reporte_mermas = [] 

                for item in mochila:
                    cant_devuelta = int(request.POST.get(f'devuelto_{item.id}', 0)) # Buenos
                    cant_merma = int(request.POST.get(f'merma_{item.id}', 0))       # Malos
                    
                    total_salida = cant_devuelta + cant_merma
                    
                    # Cálculo del Consumo (Lo que no devolvió ni rompió, se asume instalado)
                    # OJO: Si es herramienta, el consumo es 0 logicamente, pero matemáticamente es la diferencia
                    consumo_calculado = item.cantidad - total_salida

                    if total_salida > item.cantidad:
                        raise ValueError(f"Error en {item.producto.nombre}: Devolución excede stock.")

                    # 2. GUARDAR EL DETALLE COMPLETO EN EL DOCUMENTO (Para el PDF)
                    # Usamos los campos que ya existen en tu modelo DocumentoItem
                    DocumentoItem.objects.create(
                        documento=doc_ing,
                        producto=item.producto,
                        cantidad=cant_devuelta,        # Lo que entra al almacén físicamente
                        cantidad_usada=consumo_calculado, # Lo que se instaló
                        cantidad_merma=cant_merma,     # Lo que se rompió
                        observacion="Liq. Técnico"
                    )

                    # 3. MOVER STOCK FÍSICO DE ALMACÉN (Solo lo bueno entra)
                    if cant_devuelta > 0:
                        MovimientoInventario.objects.create(
                            producto=item.producto,
                            sede=sede_almacen,
                            tipo=MovimientoInventario.TIPO_IN,
                            qty=cant_devuelta,
                            referencia=doc_ing.numero,
                            usuario=request.user,
                            nota=f"Retorno Liq. {tecnico.username}"
                        ).aplicar()
                    
                    # 4. ACTUALIZAR STOCK DEL TÉCNICO (Mochila)
                    if item.producto.es_activo:
                        # Herramienta: Solo baja lo que devolvió o rompió
                        item.cantidad -= total_salida
                        if item.cantidad == 0: item.delete()
                        else: item.save()
                    else:
                        # Consumible: Se pone a CERO (todo lo que no volvió se considera consumido)
                        item.cantidad = 0 
                        item.save()

                    if cant_merma > 0:
                        reporte_mermas.append(f"{cant_merma}x {item.producto.nombre}")

                # Actualizar observaciones con mermas
                if reporte_mermas:
                    doc_ing.observaciones += " | MERMAS: " + ", ".join(reporte_mermas)
                    doc_ing.save()

                messages.success(request, f"✅ Liquidación registrada correctamente. Doc: {doc_ing.numero}")
                return redirect('liquidacion_tecnico_lista')

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

    return render(request, 'operaciones/liquidar_tecnico_form.html', {
        'tecnico': tecnico,
        'mochila': mochila
    })


@login_required
def tecnico_mi_stock(request):
    """
    Muestra el inventario actual en poder del técnico (Mochila),
    separado en Herramientas (Activos) y Materiales (Consumibles).
    """
    # 1. Seguridad: Solo técnicos o jefes
    _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    
    # 2. Consultar todo el stock positivo
    stock_qs = StockTecnico.objects.filter(
        tecnico=request.user, 
        cantidad__gt=0
    ).select_related('producto').order_by('producto__nombre')
    
    # 3. Separar en dos listas (Python lo hace rápido en memoria)
    herramientas = []
    materiales = []
    
    for item in stock_qs:
        if item.producto.es_activo:
            herramientas.append(item)
        else:
            materiales.append(item)
    
    return render(request, 'operaciones/tecnico_mi_stock.html', {
        'herramientas': herramientas,
        'materiales': materiales
    })


# ... importaciones existentes ...

@login_required
def liquidacion_tecnico_print(request, doc_id):
    """
    Vista de impresión para la liquidación semanal (Documento ING).
    """
    # Buscamos el documento por ID
    doc = get_object_or_404(DocumentoInventario, id=doc_id)
    
    # Seguridad básica: verificar que sea un documento de la sede o usuario adecuado
    # (Opcional, pero recomendado si quieres restringir)
    
    # Obtenemos los items devueltos (los que están en DocumentoItem)
    items = doc.items.select_related('producto').order_by('producto__nombre')
    
    return render(request, 'operaciones/pdf_liquidacion_tecnico.html', {
        'doc': doc,
        'items': items,
    })