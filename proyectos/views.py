from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Sum, F
from decimal import Decimal
import datetime
from inventario.models import Stock
from django.db import transaction
from inventario.models import (
    UserProfile, DocumentoInventario, DocumentoItem, Stock, 
    TipoDocumento, EstadoDocumento
)
# Importamos modelos locales
from .models import Proyecto, ProyectoMaterial, ProyectoAsignacion, EstadoProyecto
# ✅ IMPORTANTE: Agregamos ProyectoMaterialForm aquí abajo 👇
from .forms import ProyectoForm, ProyectoMaterialForm 

# Importamos modelos del core
from inventario.models import UserProfile

from .utils import render_to_pdf
from django.utils import timezone
from django.http import HttpResponse

# ==========================================
# 🎨 ZONA DEL DISEÑADOR / PLANIFICADOR
# ==========================================

@login_required
def disenador_dashboard(request):
    """
    Panel principal para el Rol DISEÑADOR.
    Ve sus proyectos creados y el estado general.
    """
    profile = request.user.profile
    if profile.rol not in [UserProfile.Rol.DISENADOR, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA]:
        return redirect('home')

    if profile.rol == UserProfile.Rol.DISENADOR:
        proyectos = Proyecto.objects.filter(creado_por=request.user).order_by('-creado_en')
    else:
        proyectos = Proyecto.objects.all().order_by('-creado_en')

    context = {
        'proyectos': proyectos,
        'total_activos': proyectos.exclude(estado=EstadoProyecto.FINALIZADO).count()
    }
    return render(request, 'proyectos/disenador_dashboard.html', context)

@login_required
def proyecto_create(request):
    """
    Vista para crear un NUEVO proyecto y subir el plano.
    Genera el código automático OBRA-AAAA-XXX
    """
    if request.method == 'POST':
        form = ProyectoForm(request.POST, request.FILES) 
        if form.is_valid():
            proyecto = form.save(commit=False)
            
            # 1. Asignar creador y estado
            proyecto.creado_por = request.user
            proyecto.estado = EstadoProyecto.PENDIENTE
            
            # 2. 🤖 GENERACIÓN AUTOMÁTICA DE CÓDIGO
            # Formato: OBRA-2026-0001
            year = datetime.date.today().year
            ultimo_proyecto = Proyecto.objects.filter(codigo__startswith=f"OBRA-{year}").order_by('id').last()
            
            if ultimo_proyecto:
                # Si existe OBRA-2026-005, sacamos el 5 y sumamos 1
                try:
                    correlativo = int(ultimo_proyecto.codigo.split('-')[-1]) + 1
                except ValueError:
                    correlativo = 1
            else:
                correlativo = 1
            
            proyecto.codigo = f"OBRA-{year}-{correlativo:04d}"
            
            # 3. Guardar
            proyecto.save()
            
            messages.success(request, f'Proyecto "{proyecto.nombre}" creado con código {proyecto.codigo}.')
            return redirect('proyecto_materiales', proyecto_id=proyecto.id)
        else:
            messages.error(request, 'Error en el formulario. Revisa los campos.')
    else:
        form = ProyectoForm()

    return render(request, 'proyectos/proyecto_form.html', {'form': form})


@login_required
def proyecto_materiales(request, proyecto_id):
    """
    Paso 2: Gestionar la lista de materiales (Receta) del proyecto.
    """
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    
    # Manejar el formulario de agregar material
    if request.method == 'POST':
        form = ProyectoMaterialForm(request.POST)
        if form.is_valid():
            nuevo_material = form.save(commit=False)
            nuevo_material.proyecto = proyecto
            
            # Evitar duplicados: Si ya existe el producto, sumamos la cantidad
            existente = ProyectoMaterial.objects.filter(proyecto=proyecto, producto=nuevo_material.producto).first()
            
            if existente:
                existente.cantidad_planificada += nuevo_material.cantidad_planificada
                existente.save()
                messages.success(request, f"Se actualizó la cantidad de {nuevo_material.producto.nombre}.")
            else:
                nuevo_material.save()
                messages.success(request, f"{nuevo_material.producto.nombre} agregado al proyecto.")
            
            return redirect('proyecto_materiales', proyecto_id=proyecto.id)
        else:
            messages.error(request, "Error al agregar material.")
    else:
        form = ProyectoMaterialForm()

    # Listar materiales ya agregados
    materiales = proyecto.materiales.select_related('producto').all()

    return render(request, 'proyectos/materiales_form.html', {
        'proyecto': proyecto,
        'form': form,
        'materiales': materiales
    })

@login_required
def eliminar_material_proyecto(request, item_id):
    """Borrar un material de la lista de planificación"""
    item = get_object_or_404(ProyectoMaterial, id=item_id)
    proyecto_id = item.proyecto.id # Guardamos el ID para volver
    item.delete()
    messages.success(request, "Material eliminado del proyecto.")
    return redirect('proyecto_materiales', proyecto_id=proyecto_id)

# ==========================================
# 🌍 VISTAS GENERALES (Listado y Detalle)
# ==========================================

@login_required
def proyecto_list(request):
    """Lista todos los proyectos visibles según permisos"""
    profile = getattr(request.user, 'profile', None)
    if not profile:
        return redirect('home')

    qs = Proyecto.objects.all().select_related('sede', 'creado_por')

    if profile.rol == UserProfile.Rol.ALMACEN:
        sede = profile.get_sede_operativa()
        if sede:
            qs = qs.filter(sede=sede)
    
    elif profile.rol == UserProfile.Rol.SOLICITANTE:
        # Aquí filtramos por el campo 'responsable' directo o por asignaciones extra
        qs = qs.filter(responsable=request.user)

    context = {
        'proyectos': qs.order_by('-creado_en'),
        'estados': EstadoProyecto.choices
    }
    return render(request, 'proyectos/lista.html', context)

@login_required
def proyecto_detail(request, pk):
    """Ver detalle del proyecto, materiales y técnicos"""
    proyecto = get_object_or_404(Proyecto, pk=pk)
    
    materiales = proyecto.materiales.select_related('producto').order_by('producto__nombre')
    
    # Asignaciones extra (si usas la tabla ProyectoAsignacion)
    # Si solo usas 'responsable', esto puede quedar vacío o mostrar colaboradores extra
    tecnicos = proyecto.asignaciones_extra.filter(activo=True).select_related('tecnico')

    # Calcular costo real acumulado
    costo_total = Decimal("0.00")
    for mat in materiales:
        costo_total += mat.costo_total_real

    context = {
        'proyecto': proyecto,
        'materiales': materiales,
        'tecnicos': tecnicos,
        'costo_total': costo_total
    }
    return render(request, 'proyectos/detalle.html', context)

@login_required
def editar_cantidad_material(request, item_id):
    """Permite editar la cantidad de un material directamente desde la tabla"""
    if request.method == 'POST':
        item = get_object_or_404(ProyectoMaterial, id=item_id)
        nueva_cantidad = request.POST.get('nueva_cantidad')
        
        try:
            nueva_cantidad = int(nueva_cantidad)
            if nueva_cantidad > 0:
                item.cantidad_planificada = nueva_cantidad
                item.save()
                messages.success(request, "Cantidad actualizada.")
            else:
                # Si pone 0 o negativo, lo borramos? Mejor damos error.
                messages.error(request, "La cantidad debe ser mayor a 0.")
        except ValueError:
            messages.error(request, "Número inválido.")
            
        return redirect('proyecto_materiales', proyecto_id=item.proyecto.id)
    
    return redirect('disenador_dashboard')

@login_required
def almacen_proyectos_list(request):
    """
    Vista para que el ALMACENERO vea los proyectos activos en SU sede
    y pueda entrar a despachar materiales.
    """
    profile = getattr(request.user, 'profile', None)
    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        messages.error(request, "Acceso exclusivo para Almacén.")
        return redirect('home')

    sede_almacen = profile.get_sede_operativa()

    # Filtramos proyectos que:
    # 1. Sean de la misma sede que el almacenero
    # 2. No estén finalizados ni anulados
    proyectos = Proyecto.objects.filter(
        sede=sede_almacen
    ).exclude(
        estado__in=[EstadoProyecto.FINALIZADO, EstadoProyecto.ANULADO]
    ).order_by('-creado_en')

    return render(request, 'proyectos/almacen_proyectos_list.html', {
        'proyectos': proyectos
    })


@login_required
def almacen_proyecto_detalle(request, proyecto_id):
    """
    Vista donde el Almacenero ve la 'Receta' y decide despachar.
    """
    profile = getattr(request.user, 'profile', None)
    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return redirect('home')

    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    materiales = proyecto.materiales.select_related('producto').all()

    for item in materiales:
        stock_item = Stock.objects.filter(producto=item.producto, sede=proyecto.sede).first()
        item.stock_actual = stock_item.cantidad if stock_item else 0

    # Cálculo simple de progreso
    total_items = materiales.count()
    items_completos = 0
    for m in materiales:
        if m.cantidad_entregada >= m.cantidad_planificada:
            items_completos += 1
    
    progreso = (items_completos / total_items * 100) if total_items > 0 else 0

    return render(request, 'proyectos/almacen_proyecto_detalle.html', {
        'proyecto': proyecto,
        'materiales': materiales,
        'progreso': int(progreso)
    })

@login_required
def almacen_generar_salida(request, proyecto_id):
    """
    Vista Intermedia: El almacenero confirma CUÁNTO va a despachar realmente.
    """
    # 1. Seguridad: Solo Almacén
    profile = getattr(request.user, 'profile', None)
    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return redirect('home')

    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    sede = proyecto.sede
    
    # 2. Calcular Items Pendientes y Stock Disponible
    materiales = proyecto.materiales.select_related('producto').all()
    items_pendientes = []
    
    for m in materiales:
        pendiente = m.cantidad_planificada - m.cantidad_entregada
        if pendiente > 0:
            # Buscamos stock real en la sede
            stock_obj = Stock.objects.filter(producto=m.producto, sede=sede).first()
            stock_actual = stock_obj.cantidad if stock_obj else 0
            
            # Calculamos cuánto sugerir
            sugerido = min(pendiente, stock_actual)
            
            m.stock_temp = stock_actual 
            m.pendiente_temp = pendiente
            m.sugerido = sugerido
            items_pendientes.append(m)

    # 🛑 PORTERO INTELIGENTE
    if not items_pendientes:
        messages.success(request, "🎉 ¡Excelente! Este proyecto ya está completado al 100%.")
        return redirect('almacen_proyecto_detalle', proyecto_id=proyecto.id)

    # 3. Procesar el Formulario (POST)
    if request.method == 'POST':
        try:
            with transaction.atomic():
                # A. Crear Cabecera (BORRADOR)
                doc = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.SAL, 
                    estado=EstadoDocumento.BORRADOR, 
                    sede=sede,
                    responsable=request.user, 
                    solicitante=proyecto.responsable, 
                    referencia=proyecto.codigo,
                    observaciones=request.POST.get('notas', '')
                )

                # B. Procesar Items seleccionados
                hubo_movimiento = False
                
                for m in items_pendientes:
                    cantidad_a_despachar = int(request.POST.get(f'input_{m.id}', 0))
                    
                    if cantidad_a_despachar > 0:
                        # Validación de seguridad
                        if cantidad_a_despachar > m.stock_temp:
                            raise ValueError(f"Stock insuficiente para {m.producto.nombre}")

                        # ✅ CORRECCIÓN CRÍTICA: Usamos DocumentoItem
                        DocumentoItem.objects.create(
                            documento=doc,
                            producto=m.producto,
                            cantidad=cantidad_a_despachar
                        )
                        
                        # Actualizar contador del proyecto
                        m.cantidad_entregada += cantidad_a_despachar
                        m.save()
                        hubo_movimiento = True

                # C. Confirmar Documento
                if hubo_movimiento:
                    # Esto llama a tu método en models.py que mueve el Kardex y Stock
                    doc.confirmar()
                    
                    # Actualizar estado del proyecto
                    if proyecto.estado == EstadoProyecto.PENDIENTE:
                        proyecto.estado = EstadoProyecto.EN_PROCESO
                        proyecto.save()
                        
                    messages.success(request, f"✅ Despacho {doc.numero} realizado con éxito.")
                    return redirect('almacen_proyecto_detalle', proyecto_id=proyecto.id)
                else:
                    messages.warning(request, "⚠️ No seleccionaste ninguna cantidad para despachar.")
                    doc.delete() # Borrar borrador vacío

        except ValueError as e:
            messages.error(request, str(e))
        except Exception as e:
            messages.error(request, f"Error inesperado al procesar salida: {str(e)}")

    # 4. Renderizar Vista (GET)
    return render(request, 'proyectos/almacen_generar_salida.html', {
        'proyecto': proyecto,
        'items': items_pendientes
    })


@login_required
def eliminar_proyecto(request, pk):
    """
    Elimina un proyecto y sus materiales planificados.
    """
    proyecto = get_object_or_404(Proyecto, pk=pk)
    
    # Seguridad: Solo el dueño, Admin o Jefa pueden borrar
    profile = request.user.profile
    es_dueno = proyecto.creado_por == request.user
    es_admin = profile.rol in [UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA]
    
    if not (es_dueno or es_admin):
        messages.error(request, "No tienes permiso para eliminar este proyecto.")
        return redirect('disenador_dashboard')

    if request.method == 'POST':
        nombre = proyecto.nombre
        proyecto.delete()
        messages.success(request, f"🗑️ Proyecto '{nombre}' eliminado correctamente.")
        return redirect('disenador_dashboard')
    
    # Si intentan entrar por GET, los mandamos de vuelta
    return redirect('disenador_dashboard')

@login_required
def proyecto_pdf_salida(request, proyecto_id):
    """
    Genera un PDF con el resumen de materiales entregados para firmar.
    """
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    
    # Filtramos solo lo que ya se entregó (mayor a 0)
    materiales_entregados = []
    for m in proyecto.materiales.select_related('producto').all():
        if m.cantidad_entregada > 0:
            materiales_entregados.append(m)

    data = {
        'proyecto': proyecto,
        'materiales': materiales_entregados,
        'fecha_impresion': timezone.now(),
        'usuario': request.user,
        'host': request.get_host(), # Para rutas absolutas si pones imágenes
    }
    
    pdf = render_to_pdf('proyectos/pdf_vale_salida.html', data)
    
    if pdf:
        # Esto hace que se descargue con el nombre correcto
        response = HttpResponse(pdf, content_type='application/pdf')
        filename = f"Vale_Salida_{proyecto.codigo}.pdf"
        content = f"inline; filename={filename}"
        response['Content-Disposition'] = content
        return response
    
    return HttpResponse("Error al generar el PDF", status=404)

@login_required
def almacen_liquidacion_lista(request):
    """
    Muestra solo los proyectos que están EN PROCESO (ya se despacharon materiales)
    y están listos para recibir devoluciones/liquidación.
    """
    profile = getattr(request.user, 'profile', None)
    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return redirect('home')

    # Filtramos solo los que están 'EN_PROCESO'
    proyectos = Proyecto.objects.filter(
        sede=profile.get_sede_operativa(),
        estado=EstadoProyecto.EN_PROCESO
    ).select_related('responsable').order_by('-creado_en')

    return render(request, 'proyectos/almacen_liquidacion_lista.html', {
        'proyectos': proyectos
    })

@login_required
def almacen_liquidar_proyecto(request, proyecto_id):
    profile = getattr(request.user, 'profile', None)
    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return redirect('home')

    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    
    # 1. Filtramos materiales entregados
    materiales = []
    for m in proyecto.materiales.select_related('producto').all():
        if m.cantidad_entregada > 0:
            m.max_devolucion = m.cantidad_entregada - (m.cantidad_devuelta + m.cantidad_merma)
            if m.max_devolucion > 0:
                materiales.append(m)

    if request.method == 'POST':
        try:
            with transaction.atomic():
                doc_ing = None
                hubo_buenos = False
                
                def get_doc_ing():
                    return DocumentoInventario.objects.create(
                        tipo=TipoDocumento.ING,
                        estado=EstadoDocumento.BORRADOR,
                        sede=proyecto.sede,
                        responsable=request.user,
                        solicitante=proyecto.responsable,
                        referencia=f"RETORNO {proyecto.codigo}",
                        observaciones=request.POST.get('notas', '')
                    )

                for m in materiales:
                    cant_buena = int(request.POST.get(f'input_good_{m.id}', 0))
                    cant_mala = int(request.POST.get(f'input_bad_{m.id}', 0))
                    total_retorno = cant_buena + cant_mala
                    
                    # Validación de seguridad
                    if total_retorno > m.max_devolucion:
                        raise ValueError(f"Estás devolviendo más de lo pendiente en {m.producto.nombre}")

                    # A. Si hay retorno BUENO -> Documento de Ingreso
                    if cant_buena > 0:
                        if not doc_ing: doc_ing = get_doc_ing()
                        DocumentoItem.objects.create(
                            documento=doc_ing,
                            producto=m.producto,
                            cantidad=cant_buena,
                            observacion="Retorno de Obra (Buen estado)"
                        )
                        hubo_buenos = True

                    # B. Actualizamos los contadores del Material
                    # ⚠️ ESTO AHORA SE EJECUTA SIEMPRE, AUNQUE EL RETORNO SEA 0
                    m.cantidad_devuelta += cant_buena
                    m.cantidad_merma += cant_mala
                    
                    # C. CÁLCULO FINAL (MATEMÁTICA PURA)
                    # Usado = Entregado - (Todo lo que devolvió + Todo lo que se rompió)
                    m.cantidad_usada = m.cantidad_entregada - (m.cantidad_devuelta + m.cantidad_merma)
                    
                    # Guardamos el costo unitario si no existe (para reporte financiero)
                    if not m.costo_unitario and m.producto.precio:
                         m.costo_unitario = m.producto.precio

                    m.save()

                # 2. Confirmar Ingreso (Si hubo algo bueno)
                if hubo_buenos and doc_ing:
                    doc_ing.confirmar()
                    messages.success(request, f"✅ Material recuperado al stock (Doc: {doc_ing.numero}).")
                
                # 3. Cerrar Proyecto
                proyecto.estado = EstadoProyecto.FINALIZADO
                proyecto.fin = timezone.now()
                proyecto.save()
                
                messages.success(request, f"🏁 Proyecto {proyecto.codigo} LIQUIDADO y CERRADO.")
                return redirect('almacen_liquidacion_lista')

        except Exception as e:
            print(f"Error: {e}")
            messages.error(request, f"Error al liquidar: {str(e)}")

    return render(request, 'proyectos/almacen_liquidar_proyecto.html', {
        'proyecto': proyecto,
        'materiales': materiales
    })

@login_required
def proyecto_pdf_liquidacion(request, proyecto_id):
    """
    Genera el Acta de Liquidación / Cierre de Obra.
    SIN COSTOS, SOLO CANTIDADES FÍSICAS.
    """
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    
    # Validamos estado
    if proyecto.estado == EstadoProyecto.PENDIENTE:
        return HttpResponse("El proyecto aún no está en etapa de liquidación.", status=400)

    # Filtramos materiales y CALCULAMOS EL TOTAL DE CONSUMO
    materiales = []
    total_consumido = 0  # 👈 Variable acumuladora
    
    for m in proyecto.materiales.select_related('producto').all():
        if m.cantidad_entregada > 0:
            materiales.append(m)
            # Sumamos lo que se quedó instalado (Consumo)
            total_consumido += m.cantidad_usada

    data = {
        'proyecto': proyecto,
        'materiales': materiales,
        'total_consumido': total_consumido, # 👈 Pasamos el total al template
        'fecha_impresion': timezone.now(),
        'usuario': request.user,
        'host': request.get_host(),
    }
    
    pdf = render_to_pdf('proyectos/pdf_acta_liquidacion.html', data)
    
    if pdf:
        response = HttpResponse(pdf, content_type='application/pdf')
        filename = f"Acta_Cierre_{proyecto.codigo}.pdf"
        response['Content-Disposition'] = f"inline; filename={filename}"
        return response
    
    return HttpResponse("Error al generar el PDF", status=404)

@login_required
def almacen_historial_obras(request):
    """
    Muestra el historial de todas las obras FINALIZADAS con opción a reimprimir Acta.
    """
    profile = getattr(request.user, 'profile', None)
    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return redirect('home')

    # Filtramos solo los terminados
    proyectos_cerrados = Proyecto.objects.filter(
        sede=profile.get_sede_operativa(),
        estado=EstadoProyecto.FINALIZADO
    ).select_related('responsable').order_by('-fin') # Ordenado por fecha de cierre (reciente primero)

    return render(request, 'proyectos/almacen_historial_lista.html', {
        'proyectos': proyectos_cerrados
    })

@login_required
def admin_reporte_lista(request):
    """
    Vista Gerencial: Lista de todos los proyectos con su COSTO TOTAL calculado.
    """
    profile = getattr(request.user, 'profile', None)
    # Validamos que sea ADMIN o SUPERUSUARIO
    if not profile or profile.rol not in [UserProfile.Rol.ADMIN]:
        if not request.user.is_superuser: # Dejamos pasar al superuser también
            return redirect('home')

    # Traemos todos los proyectos (sin importar estado)
    proyectos = Proyecto.objects.select_related('responsable', 'sede').order_by('-creado_en')

    return render(request, 'proyectos/admin_reporte_lista.html', {
        'proyectos': proyectos
    })

@login_required
def admin_detalle_financiero(request, proyecto_id):
    """
    Vista Gerencial Detallada: Muestra materiales CON PRECIOS y el balance final.
    """
    profile = getattr(request.user, 'profile', None)
    if not profile or profile.rol not in [UserProfile.Rol.ADMIN]:
        if not request.user.is_superuser:
            return redirect('home')

    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    materiales = proyecto.materiales.select_related('producto').all()

    return render(request, 'proyectos/admin_detalle_financiero.html', {
        'proyecto': proyecto,
        'materiales': materiales
    })