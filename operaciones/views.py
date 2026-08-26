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
from inventario.models import Sede, Producto, Stock, UserProfile, DocumentoInventario, TipoDocumento, EstadoDocumento, StockTecnico, ItemSerializado
# Importamos modelos de esta app
from operaciones.models import LiquidacionSemanal, LiquidacionLog
# Importamos servicios
from operaciones.services import LiquidacionService

from proyectos.models import Proyecto, EstadoProyecto, AsignacionCuadrilla

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
    Dashboard del técnico.
    """
    profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    sede = profile.get_sede_operativa()
    
    # KPIs existentes
    reqs_qs = DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ, responsable=request.user)
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

    proyectos_asignados = Proyecto.objects.filter(
        responsable=request.user
    ).exclude(
        estado__in=[EstadoProyecto.FINALIZADO, EstadoProyecto.ANULADO]
    ).order_by('estado', '-creado_en')

    # 🚀 NUEVO 1: Historial de Liquidaciones del técnico
    # Buscamos los INGRESOS (ING) que tengan referencia LIQ-SEMANAL y donde él sea el solicitante
    liquidaciones_historial = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.ING,
        referencia="LIQ-SEMANAL",
        solicitante=request.user
    ).order_by('-fecha')[:10] # Mostramos las últimas 10

    # 🚀 NUEVO 2: Traer la mochila (Stock) directamente al dashboard
    stock_qs = StockTecnico.objects.filter(
        tecnico=request.user, 
        cantidad__gt=0
    ).select_related('producto').order_by('producto__nombre')
    
    herramientas = []
    materiales = []
    
    for item in stock_qs:
        if item.producto.es_activo:
            herramientas.append(item)
        else:
            materiales.append(item)

    return render(request, "operaciones/tecnico_dashboard.html", {
        "sede": sede, 
        "kpis": kpis, 
        "proyectos": proyectos_asignados,
        "liquidaciones_historial": liquidaciones_historial, # Pasamos liquidaciones
        "herramientas": herramientas,                       # Pasamos herramientas
        "materiales": materiales                            # Pasamos materiales
    })

@login_required
def tecnico_mis_entregas(request):
    """
    Lista de entregas (SAL) recibidas por el técnico.
    """
    _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    
    # Buscamos SAL Confirmadas donde el técnico sea el solicitante (directo) 
    # o el responsable del REQ original.
    sals = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.SAL,
        estado=EstadoDocumento.CONFIRMADO
    ).filter(
        models.Q(solicitante=request.user) |
        models.Q(origen__responsable=request.user) |
        models.Q(responsable=request.user)
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
    Lista de técnicos que tienen material en su poder por mochila semanal.

    Importante:
    - La liquidación semanal NO debe incluir responsables PEX cuya carga proviene
      de una obra ya finalizada/liquidada por acta.
    - Si el técnico solo tiene stock relacionado a proyectos FINALIZADOS, no debe salir aquí.
    """
    if not request.user.profile.rol in ['ALMACEN', 'ADMIN', 'JEFA']:
        return redirect('home')

    try:
        sede_actual = request.user.profile.get_sede_operativa()
    except Exception:
        messages.error(request, "No tienes una sede asignada para ver liquidaciones.")
        return redirect('dash_almacen')

    tecnicos_base = (
        User.objects.filter(
            mi_stock__cantidad__gt=0,
            mi_stock__sede=sede_actual,
        )
        .distinct()
        .order_by("username")
    )

    tecnicos_con_deuda = []

    for tecnico in tecnicos_base:
        stock_actual = StockTecnico.objects.filter(
            tecnico=tecnico,
            cantidad__gt=0,
        ).select_related("producto")

        tiene_deuda_semanal = False

        for stock in stock_actual:
            # Cantidad de ese producto que todavía está vinculada a obras PEX
            # activas/no cerradas donde el técnico es responsable o receptor.
            cantidad_en_obras_activas = AsignacionCuadrilla.objects.filter(
                producto=stock.producto,
                estado__in=["ENTREGADO", "CONSUMIDO", "MERMA", "PERDIDO"],
            ).filter(
                Q(entregado_por=tecnico) | Q(recibido_por=tecnico)
            ).exclude(
                proyecto__estado__in=[EstadoProyecto.FINALIZADO, EstadoProyecto.ANULADO]
            ).aggregate(
                total=Sum("cantidad")
            )["total"] or 0

            # Cantidad relacionada a obras ya cerradas/liquidadas.
            cantidad_en_obras_cerradas = AsignacionCuadrilla.objects.filter(
                producto=stock.producto,
                estado__in=["ENTREGADO", "CONSUMIDO", "MERMA", "PERDIDO"],
            ).filter(
                Q(entregado_por=tecnico) | Q(recibido_por=tecnico)
            ).filter(
                proyecto__estado__in=[EstadoProyecto.FINALIZADO, EstadoProyecto.ANULADO]
            ).aggregate(
                total=Sum("cantidad")
            )["total"] or 0

            # Si el stock actual queda explicado por obras cerradas,
            # no lo mandamos a liquidación semanal.
            stock_no_obra_cerrada = int(stock.cantidad or 0) - int(cantidad_en_obras_cerradas or 0)

            # Si todavía tiene mochila no explicada por obra cerrada,
            # o tiene materiales de obra activa, sí aparece.
            if stock_no_obra_cerrada > 0 or cantidad_en_obras_activas > 0:
                tiene_deuda_semanal = True
                break

        if tiene_deuda_semanal:
            tecnicos_con_deuda.append(tecnico)

    return render(request, 'operaciones/liquidacion_tecnicos_lista.html', {
        'tecnicos': tecnicos_con_deuda
    })


@login_required
def liquidar_tecnico(request, tecnico_id):
    if not request.user.profile.rol in ['ALMACEN', 'ADMIN', 'JEFA']:
        return redirect('home')

    tecnico = get_object_or_404(User, id=tecnico_id)

    sede_almacen = request.user.profile.get_sede_operativa()

    mochila = (
        StockTecnico.objects
        .filter(
            tecnico=tecnico,
            sede=sede_almacen,
            cantidad__gt=0,
        )
        .select_related("producto", "sede")
    )

    productos_mochila_ids = mochila.values_list("producto_id", flat=True)
    
    equipos_asignados = ItemSerializado.objects.filter(
        asignado_a=tecnico,
        estado=ItemSerializado.Estado.ASIGNADO,
        producto_id__in=productos_mochila_ids,
    )

    if request.method == 'POST':
        try:
            with transaction.atomic():
                notas_usuario = request.POST.get('observaciones', '')
                
                from inventario.models import Ubicacion
                ubicacion_retorno = Ubicacion.objects.filter(sede=sede_almacen).first()
                
                doc_ing = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.ING,
                    estado=EstadoDocumento.CONFIRMADO,
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
                    if item.producto.es_serializado:
                        devueltos_ids = request.POST.getlist(f'check_devuelto_{item.id}')
                        mermas_ids = request.POST.getlist(f'check_merma_{item.id}')
                        
                        cant_devuelta = len(devueltos_ids)
                        cant_merma = len(mermas_ids)
                        total_salida = cant_devuelta + cant_merma
                        consumo_calculado = item.cantidad - total_salida
                        
                        if total_salida > item.cantidad:
                            raise ValueError(f"Error en {item.producto.nombre}: Devolución excede stock.")

                        # 2. Retornar al almacén
                        if devueltos_ids:
                            ItemSerializado.objects.filter(id__in=devueltos_ids).update(
                                estado=ItemSerializado.Estado.EN_ALMACEN,
                                asignado_a=None,
                                ubicacion=ubicacion_retorno 
                            )
                            
                        # 3. Mandar a merma
                        if mermas_ids:
                            ItemSerializado.objects.filter(id__in=mermas_ids).update(
                                estado=ItemSerializado.Estado.MERMA,
                                asignado_a=None,
                                ubicacion=ubicacion_retorno 
                            )
                            
                        # ✅ CORRECCIÓN: Los que NO devolvió, solo se instalan si son consumibles
                        todos_ids = devueltos_ids + mermas_ids
                        if not item.producto.es_activo: # Si es ONU/Material, se gastó
                            ItemSerializado.objects.filter(
                                asignado_a=tecnico, 
                                producto=item.producto, 
                                estado=ItemSerializado.Estado.ASIGNADO
                            ).exclude(id__in=todos_ids).update(
                                estado=ItemSerializado.Estado.INSTALADO,
                                asignado_a=None
                            )
                        # Si es Herramienta (es_activo=True), sigue conservando su asignación normal.

                    else:
                        cant_devuelta = int(request.POST.get(f'devuelto_{item.id}', 0))
                        cant_merma = int(request.POST.get(f'merma_{item.id}', 0))
                        
                        total_salida = cant_devuelta + cant_merma
                        consumo_calculado = item.cantidad - total_salida

                        if total_salida > item.cantidad:
                            raise ValueError(f"Error en {item.producto.nombre}: Devolución excede stock.")

                    DocumentoItem.objects.create(
                        documento=doc_ing,
                        producto=item.producto,
                        cantidad=cant_devuelta,        
                        cantidad_usada=consumo_calculado, 
                        cantidad_merma=cant_merma,     
                        observacion="Liq. Técnico"
                    )

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
                    
                    if item.producto.es_activo:
                        item.cantidad -= total_salida
                        if item.cantidad == 0: item.delete()
                        else: item.save()
                    else:
                        item.cantidad = 0 
                        item.save()

                    if cant_merma > 0:
                        reporte_mermas.append(f"{cant_merma}x {item.producto.nombre}")

                if reporte_mermas:
                    doc_ing.observaciones += " | MERMAS: " + ", ".join(reporte_mermas)
                    doc_ing.save()

                messages.success(request, f"✅ Liquidación registrada correctamente. Doc: {doc_ing.numero}")
                return redirect('liquidacion_tecnico_lista')

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

    return render(request, 'operaciones/liquidar_tecnico_form.html', {
        'tecnico': tecnico,
        'mochila': mochila,
        'equipos_asignados': equipos_asignados
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

    # Jalamos el técnico que retiró el material del último despacho (Preparación
    # de materiales) que armó la mochila que ahora se está liquidando.
    ultimo_despacho = None
    if doc.solicitante:
        ultimo_despacho = (
            DocumentoInventario.objects.filter(
                tipo=TipoDocumento.SAL,
                estado=EstadoDocumento.CONFIRMADO,
                solicitante=doc.solicitante,
                sede=doc.sede,
                fecha__lte=doc.fecha,
            )
            .select_related('retirado_por')
            .order_by('-fecha')
            .first()
        )

    tecnico_retiro = ultimo_despacho.retirado_por if ultimo_despacho else None

    return render(request, 'operaciones/pdf_liquidacion_tecnico.html', {
        'doc': doc,
        'items': items,
        'tecnico_retiro': tecnico_retiro,
    })

@login_required
def tecnico_dashboard(request):
    """
    Dashboard del técnico.
    """
    profile = _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    sede = profile.get_sede_operativa()
    
    reqs_qs = DocumentoInventario.objects.filter(tipo=TipoDocumento.REQ, responsable=request.user)
    
    kpis = {
        "reqs_activos": reqs_qs.filter(estado__in=[EstadoDocumento.REQ_BORRADOR, EstadoDocumento.REQ_PENDIENTE]).count(),
        "reqs_atendidos": reqs_qs.filter(estado=EstadoDocumento.REQ_ATENDIDO).count(),
        "entregas": DocumentoInventario.objects.filter(
            tipo=TipoDocumento.SAL, 
            estado=EstadoDocumento.CONFIRMADO
        ).filter(
            models.Q(solicitante=request.user) | 
            models.Q(responsable=request.user) | 
            models.Q(origen__responsable=request.user)
        ).count(),
    }

    proyectos_asignados = Proyecto.objects.filter(
        responsable=request.user
    ).exclude(
        estado__in=[EstadoProyecto.FINALIZADO, EstadoProyecto.ANULADO]
    ).order_by('estado', '-creado_en')

    liquidaciones_historial = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.ING,
        referencia="LIQ-SEMANAL",
        solicitante=request.user
    )

    # =========================================================
    # MAGIA: Traemos la mochila y le pegamos los números físicos
    # =========================================================
    stock_qs = StockTecnico.objects.filter(
        tecnico=request.user, 
        cantidad__gt=0
    ).select_related('producto').order_by('producto__nombre')
    
    equipos_fisicos = ItemSerializado.objects.filter(
        asignado_a=request.user,
        estado=ItemSerializado.Estado.ASIGNADO
    ).select_related('producto')
    
    herramientas = []
    materiales = []
    
    for item in stock_qs:
        sus_equipos = [e for e in equipos_fisicos if e.producto.id == item.producto.id]
        
        # Armamos el paquete que el HTML va a leer
        item_data = {
            'producto': item.producto,
            'cantidad': item.cantidad,
            'equipos': sus_equipos
        }
        
        if item.producto.es_activo:
            herramientas.append(item_data)
        else:
            materiales.append(item_data)

    return render(request, "operaciones/tecnico_dashboard.html", {
        "sede": sede, 
        "kpis": kpis, 
        "proyectos": proyectos_asignados,
        "liquidaciones_historial": liquidaciones_historial,
        "herramientas": herramientas,
        "materiales": materiales
    })

@login_required
def proyecto_asignar_cuadrilla(request, proyecto_id):
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)

    # 1. Seguridad: Solo el responsable de la obra puede repartir
    if request.user != proyecto.responsable:
        messages.error(request, "Solo el responsable asignado puede repartir materiales a la cuadrilla.")
        return redirect('tecnico_dashboard')

    # 2. Obtener el equipo de trabajo (excluyendo al responsable)
    tecnicos_disponibles = User.objects.exclude(id=request.user.id).filter(is_active=True)

    # 3. Lo que tiene en su poder actualmente
    mi_stock = StockTecnico.objects.filter(tecnico=request.user, cantidad__gt=0).select_related('producto')
    mis_equipos = ItemSerializado.objects.filter(asignado_a=request.user, estado=ItemSerializado.Estado.ASIGNADO)

    if request.method == 'POST':
        receptor_id = request.POST.get('receptor_id')
        if not receptor_id:
            messages.error(request, "Debes seleccionar a un técnico.")
            return redirect('proyecto_asignar_cuadrilla', proyecto_id=proyecto.id)
            
        receptor = get_object_or_404(User, id=receptor_id)
        hubo_transferencia = False

        try:
            with transaction.atomic():
                for stock in mi_stock:
                    qty = int(request.POST.get(f'qty_{stock.producto.id}', 0))
                    
                    if qty > 0:
                        if qty > stock.cantidad:
                            raise ValueError(f"No tienes suficiente {stock.producto.nombre} para transferir.")

                        # Crear el vale de transferencia
                        asignacion = AsignacionCuadrilla.objects.create(
                            proyecto=proyecto, entregado_por=request.user,
                            recibido_por=receptor, producto=stock.producto, cantidad=qty
                        )

                        if stock.producto.es_serializado:
                            macs_seleccionadas = request.POST.getlist(f'macs_{stock.producto.id}')
                            if len(macs_seleccionadas) != qty:
                                raise ValueError(f"Debes marcar exactamente {qty} equipos/MACs de {stock.producto.nombre}.")
                            
                            for equipo_id in macs_seleccionadas:
                                equipo = ItemSerializado.objects.get(id=equipo_id, asignado_a=request.user)
                                # Cambiamos de dueño
                                equipo.asignado_a = receptor
                                equipo.save()
                                asignacion.seriales.add(equipo)
                        
                        # ✅ CORRECCIÓN CRÍTICA: Restar cantidad numérica SIEMPRE (sea cable u ONU)
                        stock.cantidad -= qty
                        if stock.cantidad == 0:
                            stock.delete()
                        else:
                            stock.save()
                        
                        # Sumar cantidad numérica al receptor
                        stock_receptor, _ = StockTecnico.objects.get_or_create(tecnico=receptor, producto=stock.producto)
                        stock_receptor.cantidad += qty
                        stock_receptor.save()
                            
                        hubo_transferencia = True

                if hubo_transferencia:
                    messages.success(request, f"Materiales transferidos exitosamente a {receptor.username}.")
                    return redirect('tecnico_dashboard')
                else:
                    messages.warning(request, "No ingresaste ninguna cantidad para transferir.")

        except Exception as e:
            messages.error(request, f"Error en la transferencia: {str(e)}")

    return render(request, 'operaciones/asignar_cuadrilla.html', {
        'proyecto': proyecto,
        'tecnicos': tecnicos_disponibles,
        'mi_stock': mi_stock,
        'mis_equipos': mis_equipos
    })

@login_required
def tecnico_mis_liquidaciones(request):
    """
    Lista el historial dedicado de liquidaciones semanales del técnico.
    """
    _require_roles(request.user, UserProfile.Rol.SOLICITANTE, UserProfile.Rol.JEFA)
    
    # Buscamos los ingresos por liquidación donde él sea el solicitante
    liquidaciones = DocumentoInventario.objects.filter(
        tipo=TipoDocumento.ING,
        referencia="LIQ-SEMANAL",
        solicitante=request.user
    ).order_by("-fecha")

    return render(request, "operaciones/tecnico_mis_liquidaciones.html", {"liquidaciones": liquidaciones})