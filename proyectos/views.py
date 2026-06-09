from django.shortcuts import render, get_object_or_404, redirect
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.contrib import messages
from django.db.models import Sum, F, Q
from decimal import Decimal
import datetime
from inventario.models import Stock
from django.db import transaction
from inventario.models import (
    UserProfile, DocumentoInventario, DocumentoItem, Stock,
    TipoDocumento, EstadoDocumento, ItemSerializado, StockTecnico, MovimientoInventario
)
# Importamos modelos locales
from .models import Proyecto, ProyectoMaterial, ProyectoAsignacion, EstadoProyecto, AsignacionCuadrilla, EstadoTransferenciaCuadrilla
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
    if request.method == 'POST':
        form = ProyectoForm(request.POST, request.FILES) 
        if form.is_valid():
            proyecto = form.save(commit=False)
            
            proyecto.creado_por = request.user
            # ✅ CORRECCIÓN: Nace en estado DISEÑO (Antes PENDIENTE)
            proyecto.estado = EstadoProyecto.DISENO
            
            # Generación de Código
            year = datetime.date.today().year
            ultimo_proyecto = Proyecto.objects.filter(codigo__startswith=f"OBRA-{year}").order_by('id').last()
            
            if ultimo_proyecto:
                try:
                    correlativo = int(ultimo_proyecto.codigo.split('-')[-1]) + 1
                except ValueError:
                    correlativo = 1
            else:
                correlativo = 1
            
            proyecto.codigo = f"OBRA-{year}-{correlativo:04d}"
            proyecto.save()
            
            messages.success(request, f'Proyecto "{proyecto.nombre}" creado. Agrega los materiales.')
            return redirect('proyecto_materiales', proyecto_id=proyecto.id)
        else:
            messages.error(request, 'Error en el formulario.')
    else:
        form = ProyectoForm()

    return render(request, 'proyectos/proyecto_form.html', {'form': form})


@login_required
def proyecto_materiales(request, proyecto_id):
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)

    if request.user != proyecto.creado_por and not request.user.is_superuser:
        messages.error(request, "No tienes permiso para editar los materiales de este proyecto.")
        return redirect("proyecto_detail", pk=proyecto.id)

    if request.method == "POST":
        if not proyecto.puede_editar_materiales:
            messages.error(request, "No puedes editar materiales en este estado.")
            return redirect("proyecto_materiales", proyecto_id=proyecto.id)

        form = ProyectoMaterialForm(request.POST)

        if form.is_valid():
            nuevo_material = form.save(commit=False)
            nuevo_material.proyecto = proyecto

            observacion_diseno = request.POST.get("observacion_diseno", "").strip()

            existente = ProyectoMaterial.objects.filter(
                proyecto=proyecto,
                producto=nuevo_material.producto,
            ).first()

            if existente:
                existente.cantidad_planificada += nuevo_material.cantidad_planificada

                if observacion_diseno:
                    existente.observacion_diseno = observacion_diseno

                existente.save()
                messages.success(request, f"Actualizado: {nuevo_material.producto.nombre}.")
            else:
                nuevo_material.observacion_diseno = observacion_diseno
                nuevo_material.save()
                messages.success(request, f"Agregado: {nuevo_material.producto.nombre}.")

            return redirect("proyecto_materiales", proyecto_id=proyecto.id)
    else:
        form = ProyectoMaterialForm()

    materiales = proyecto.materiales.select_related("producto").all()

    return render(request, "proyectos/materiales_form.html", {
        "proyecto": proyecto,
        "form": form,
        "materiales": materiales,
        "url_finalizar": "disenador_dashboard",
    })


@login_required
def eliminar_material_proyecto(request, item_id):
    item = get_object_or_404(ProyectoMaterial, id=item_id)
    proyecto = item.proyecto
    
    # ✅ Validación de estado
    if proyecto.estado not in [EstadoProyecto.DISENO, EstadoProyecto.OBSERVADO]:
        messages.error(request, "No puedes eliminar ítems en este estado.")
        return redirect('proyecto_materiales', proyecto_id=proyecto.id)

    item.delete()
    messages.success(request, "Material eliminado.")
    return redirect('proyecto_materiales', proyecto_id=proyecto.id)

# ==========================================
# 🌍 VISTAS GENERALES (Listado y Detalle)
# ==========================================

@login_required
def proyecto_list(request):
    profile = getattr(request.user, 'profile', None)
    qs = Proyecto.objects.all().select_related('sede', 'creado_por')

    if profile.rol == UserProfile.Rol.ALMACEN:
        sede = profile.get_sede_operativa()
        if sede: qs = qs.filter(sede=sede)
    
    elif profile.rol == UserProfile.Rol.SOLICITANTE:
        qs = qs.filter(responsable=request.user)

    return render(request, 'proyectos/lista.html', {
        'proyectos': qs.order_by('-creado_en'),
        'estados': EstadoProyecto.choices
    })

@login_required
def proyecto_detail(request, pk):
    proyecto = get_object_or_404(Proyecto, pk=pk)
    materiales = proyecto.materiales.select_related('producto').order_by('producto__nombre')
    tecnicos = proyecto.asignaciones_extra.filter(activo=True).select_related('tecnico')

    costo_total = sum(m.costo_total_real for m in materiales)

    return render(request, 'proyectos/detalle.html', {
        'proyecto': proyecto,
        'materiales': materiales,
        'tecnicos': tecnicos,
        'costo_total': costo_total
    })


@login_required
def editar_cantidad_material(request, item_id):
    if request.method == 'POST':
        item = get_object_or_404(ProyectoMaterial, id=item_id)
        
        # ✅ Validación de estado
        if item.proyecto.estado not in [EstadoProyecto.DISENO, EstadoProyecto.OBSERVADO]:
            messages.error(request, "No puedes editar en este estado.")
            return redirect('proyecto_materiales', proyecto_id=item.proyecto.id)

        nueva_cantidad = request.POST.get('nueva_cantidad')
        try:
            val = int(nueva_cantidad)
            if val > 0:
                item.cantidad_planificada = val
                item.save()
                messages.success(request, "Cantidad actualizada.")
        except ValueError:
            messages.error(request, "Número inválido.")
            
        return redirect('proyecto_materiales', proyecto_id=item.proyecto.id)
    
    return redirect('disenador_dashboard')


@login_required
def almacen_proyectos_list(request):
    profile = getattr(request.user, 'profile', None)
    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return redirect('home')

    sede_almacen = profile.get_sede_operativa()

    # ✅ CORRECCIÓN: Almacén solo ve lo APROBADO o lo que ya está EN PROCESO
    proyectos = Proyecto.objects.filter(
        sede=sede_almacen,
        estado__in=[EstadoProyecto.APROBADO, EstadoProyecto.EN_PROCESO]
    ).order_by('-creado_en')

    return render(request, 'proyectos/almacen_proyectos_list.html', {'proyectos': proyectos})


@login_required
def almacen_proyecto_detalle(request, proyecto_id):
    profile = getattr(request.user, 'profile', None)
    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return redirect('home')

    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    materiales = proyecto.materiales.select_related('producto').all()

    for item in materiales:
        stock_item = Stock.objects.filter(producto=item.producto, sede=proyecto.sede).first()
        item.stock_actual = stock_item.cantidad if stock_item else 0

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
    profile = getattr(request.user, 'profile', None)
    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return redirect('home')

    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    
    # ✅ Validación: Solo si está aprobado o en proceso
    if proyecto.estado not in [EstadoProyecto.APROBADO, EstadoProyecto.EN_PROCESO]:
        messages.error(request, "El proyecto no está aprobado para despacho.")
        return redirect('almacen_proyectos_list')

    sede = proyecto.sede
    materiales = proyecto.materiales.select_related('producto').all()
    items_pendientes = []
    
    for m in materiales:
        pendiente = m.cantidad_planificada - m.cantidad_entregada
        if pendiente > 0:
            stock_obj = Stock.objects.filter(producto=m.producto, sede=sede).first()
            stock_actual = stock_obj.cantidad if stock_obj else 0
            sugerido = min(pendiente, stock_actual)
            
            m.stock_temp = stock_actual 
            m.pendiente_temp = pendiente
            m.sugerido = sugerido
            items_pendientes.append(m)

    if not items_pendientes:
        messages.success(request, "Todo entregado.")
        return redirect('almacen_proyecto_detalle', proyecto_id=proyecto.id)

    if request.method == 'POST':
        try:
            with transaction.atomic():
                doc = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.SAL, 
                    estado=EstadoDocumento.BORRADOR, 
                    sede=sede,
                    responsable=request.user, 
                    solicitante=proyecto.responsable, # Jilmer recibe el stock
                    referencia=proyecto.codigo,
                    observaciones=request.POST.get('notas', '')
                )

                hubo_movimiento = False
                
                for m in items_pendientes:
                    qty = int(request.POST.get(f'input_{m.id}', 0))
                    if qty > 0:
                        if qty > m.stock_temp: 
                            raise ValueError(f"Stock insuficiente: {m.producto.nombre}")

                        if m.producto.es_serializado:
                            seriales_ingresados = request.POST.getlist(f'macs_{m.id}')
                            seriales_ingresados = [s.strip().upper() for s in seriales_ingresados if s.strip()]

                            if len(seriales_ingresados) != qty:
                                raise ValueError(f"Debes ingresar exactamente {qty} MACs/Series para {m.producto.nombre}")

                            # Validar y actualizar cada equipo en la base de datos
                            for serial in seriales_ingresados:
                                # Buscamos el equipo físico por MAC, Serial o Código de caja
                                item_fisico = ItemSerializado.objects.filter(
                                    Q(serial=serial) | Q(mac_address=serial) | Q(codigo_trazabilidad=serial),
                                    producto=m.producto,
                                    estado=ItemSerializado.Estado.EN_ALMACEN
                                ).first()

                                if not item_fisico:
                                    raise ValueError(f"El equipo con serie/MAC '{serial}' no está en el almacén o no existe.")

                                # Si lo encuentra, lo marcamos como ASIGNADO al técnico de la obra
                                item_fisico.estado = ItemSerializado.Estado.ASIGNADO
                                item_fisico.asignado_a = proyecto.responsable
                                item_fisico.save()
                        # FIN NUEVA LÓGICA ✅

                        DocumentoItem.objects.create(
                            documento=doc,
                            producto=m.producto,
                            cantidad=qty
                        )
                        m.cantidad_entregada += qty
                        m.save()
                        hubo_movimiento = True

                if hubo_movimiento:
                    doc.confirmar()
                    
                    # ✅ CORRECCIÓN: Si era el primer despacho, pasa a EN PROCESO
                    if proyecto.estado == EstadoProyecto.APROBADO:
                        proyecto.estado = EstadoProyecto.EN_PROCESO
                        proyecto.save()
                        
                    messages.success(request, f"Despacho {doc.numero} realizado.")
                    return redirect('almacen_proyecto_detalle', proyecto_id=proyecto.id)
                else:
                    messages.warning(request, "No seleccionaste cantidades.")
                    doc.delete()

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

    return render(request, 'proyectos/almacen_generar_salida.html', {
        'proyecto': proyecto,
        'items': items_pendientes
    })


@login_required
def eliminar_proyecto(request, pk):
    proyecto = get_object_or_404(Proyecto, pk=pk)
    if request.user != proyecto.creado_por and not request.user.is_superuser:
        messages.error(request, "No tienes permiso.")
        return redirect('disenador_dashboard')

    if request.method == 'POST':
        proyecto.delete()
        messages.success(request, "Proyecto eliminado.")
    return redirect('disenador_dashboard')

@login_required
def proyecto_pdf_salida(request, proyecto_id):
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    materiales_entregados = [m for m in proyecto.materiales.all() if m.cantidad_entregada > 0]
    
    return render(request, 'proyectos/pdf_vale_salida.html', {
        'proyecto': proyecto,
        'materiales': materiales_entregados,
        'fecha_impresion': timezone.now(),
        'usuario': request.user,
    })


@login_required
def almacen_liquidacion_lista(request):
    profile = request.user.profile
    if profile.rol != UserProfile.Rol.ALMACEN: return redirect('home')

    proyectos = Proyecto.objects.filter(
        sede=profile.get_sede_operativa(),
        estado=EstadoProyecto.EN_PROCESO
    ).select_related('responsable').order_by('-creado_en')

    return render(request, 'proyectos/almacen_liquidacion_lista.html', {'proyectos': proyectos})

@login_required
def almacen_liquidar_proyecto(request, proyecto_id):
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)

    profile = getattr(request.user, "profile", None)
    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        messages.error(request, "No tienes permisos para liquidar obras.")
        return redirect("home")

    if proyecto.estado != EstadoProyecto.EN_PROCESO:
        messages.warning(request, "Solo puedes liquidar proyectos en proceso.")
        return redirect("almacen_liquidacion_lista")

    materiales = []

    for m in proyecto.materiales.select_related("producto").all():
        if m.cantidad_entregada > 0:
            m.max_devolucion = m.cantidad_entregada - (m.cantidad_devuelta + m.cantidad_merma)

            if m.max_devolucion > 0:
                materiales.append(m)

    if request.method == "POST":
        try:
            with transaction.atomic():
                doc_ing = None
                hubo_buenos = False

                for m in materiales:
                    good = int(request.POST.get(f"input_good_{m.id}", 0) or 0)
                    bad = int(request.POST.get(f"input_bad_{m.id}", 0) or 0)

                    total_cierre = good + bad

                    if total_cierre > m.max_devolucion:
                        raise ValueError(
                            f"Error en {m.producto.nombre}: la devolución + merma supera lo pendiente."
                        )

                    # Actualizamos liquidación del material de obra
                    m.cantidad_devuelta += good
                    m.cantidad_merma += bad
                    m.cantidad_usada = m.cantidad_entregada - (m.cantidad_devuelta + m.cantidad_merma)

                    if not m.costo_unitario:
                        m.costo_unitario = m.producto.costo_unitario

                    m.save()

                    # Si vuelve material bueno al almacén, creamos documento ING
                    if good > 0:
                        if not doc_ing:
                            doc_ing = DocumentoInventario.objects.create(
                                tipo=TipoDocumento.ING,
                                estado=EstadoDocumento.BORRADOR,
                                sede=proyecto.sede,
                                responsable=request.user,
                                solicitante=proyecto.responsable,
                                referencia=f"RETORNO {proyecto.codigo}",
                                observaciones=f"Retorno de materiales de obra {proyecto.codigo}",
                                fecha=timezone.now(),
                            )

                        DocumentoItem.objects.create(
                            documento=doc_ing,
                            producto=m.producto,
                            cantidad=good,
                            observacion="Retorno de obra PEX",
                        )

                        hubo_buenos = True

                    # ✅ CLAVE: limpiar mochila del responsable PEX
                    # Todo lo entregado para la obra queda cerrado por acta:
                    # bueno devuelto + merma + consumido.
                    stock_tecnico = StockTecnico.objects.filter(
                        tecnico=proyecto.responsable,
                        producto=m.producto,
                    ).first()

                    if stock_tecnico:
                        cantidad_a_descontar = int(m.cantidad_entregada or 0)

                        stock_tecnico.cantidad = max(
                            int(stock_tecnico.cantidad or 0) - cantidad_a_descontar,
                            0,
                        )

                        if stock_tecnico.cantidad <= 0:
                            stock_tecnico.delete()
                        else:
                            stock_tecnico.save(update_fields=["cantidad"])

                if hubo_buenos and doc_ing:
                    doc_ing.confirmar()

                proyecto.estado = EstadoProyecto.FINALIZADO
                proyecto.fin = timezone.now()
                proyecto.save(update_fields=["estado", "fin", "actualizado_en"])

                messages.success(request, f"Proyecto {proyecto.codigo} LIQUIDADO.")
                return redirect("almacen_liquidacion_lista")

        except Exception as e:
            messages.error(request, str(e))

    return render(request, "proyectos/almacen_liquidar_proyecto.html", {
        "proyecto": proyecto,
        "materiales": materiales,
    })


@login_required
def proyecto_pdf_liquidacion(request, proyecto_id):
    """
    Genera la vista de impresión (HTML) para el Acta de Cierre.
    Usa el navegador para imprimir a PDF.
    """
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    
    # Validación simple
    if proyecto.estado == EstadoProyecto.DISENO: # O PENDIENTE en tu modelo viejo
         # Podrías redirigir o mostrar error, pero dejemos que renderice igual por si acaso
         pass

    # Calculamos totales
    materiales = []
    total_consumido = 0
    
    for m in proyecto.materiales.select_related('producto').all():
        # Solo mostramos si hubo movimiento (opcional, pero queda más limpio)
        if m.cantidad_entregada > 0:
            materiales.append(m)
            total_consumido += m.cantidad_usada

    context = {
        'proyecto': proyecto,
        'materiales': materiales,
        'total_consumido': total_consumido,
        'fecha_impresion': timezone.now(),
        'usuario': request.user,
    }
    
    # 🚀 CAMBIO CLAVE: Usamos render normal
    return render(request, 'proyectos/pdf_acta_liquidacion.html', context)


@login_required
def almacen_historial_obras(request):
    profile = request.user.profile
    proyectos = Proyecto.objects.filter(
        sede=profile.get_sede_operativa(),
        estado=EstadoProyecto.FINALIZADO
    ).select_related('responsable').order_by('-fin')
    return render(request, 'proyectos/almacen_historial_lista.html', {'proyectos': proyectos})

@login_required
def admin_reporte_lista(request):
    if not request.user.is_superuser: return redirect('home')
    proyectos = Proyecto.objects.all().order_by('-creado_en')
    return render(request, 'proyectos/admin_reporte_lista.html', {'proyectos': proyectos})

@login_required
def admin_detalle_financiero(request, proyecto_id):
    if not request.user.is_superuser: return redirect('home')
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    return render(request, 'proyectos/admin_detalle_financiero.html', {
        'proyecto': proyecto,
        'materiales': proyecto.materiales.all()
    })

@login_required
def proyecto_enviar_a_revision(request, proyecto_id):
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)

    if request.user != proyecto.creado_por and not request.user.is_superuser:
        messages.error(request, "No tienes permiso para enviar este proyecto a revisión.")
        return redirect("proyecto_detail", pk=proyecto.id)

    if not proyecto.responsable:
        messages.error(request, "Debes asignar un responsable PEX antes de enviar a revisión.")
        return redirect("proyecto_detail", pk=proyecto.id)

    if not proyecto.puede_enviar_a_revision:
        messages.error(request, "El proyecto no está listo para enviarse a revisión.")
        return redirect("proyecto_detail", pk=proyecto.id)

    proyecto.estado = EstadoProyecto.REVISION_TECNICA
    proyecto.fecha_envio_revision = timezone.now()
    proyecto.fecha_observacion = None
    proyecto.observacion_rechazo = ""
    proyecto.save(update_fields=[
        "estado",
        "fecha_envio_revision",
        "fecha_observacion",
        "observacion_rechazo",
    ])

    messages.success(
        request,
        f"Proyecto enviado a revisión técnica de {proyecto.responsable.get_full_name() or proyecto.responsable.username}.",
    )
    return redirect("disenador_dashboard")


@login_required
def proyecto_aprobar_tecnico(request, proyecto_id):
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)

    if request.user != proyecto.responsable and not request.user.is_superuser:
        messages.error(request, "Solo el responsable PEX puede aprobar este proyecto.")
        return redirect("proyecto_detail", pk=proyecto.id)

    if not proyecto.puede_aprobar_pex:
        messages.error(request, "El proyecto no está en revisión técnica.")
        return redirect("proyecto_detail", pk=proyecto.id)

    if not proyecto.materiales.exists():
        messages.error(request, "No puedes aprobar un proyecto sin materiales.")
        return redirect("proyecto_detail", pk=proyecto.id)

    proyecto.estado = EstadoProyecto.APROBADO
    proyecto.fecha_aprobacion = timezone.now()
    proyecto.fecha_observacion = None
    proyecto.observacion_rechazo = ""
    proyecto.save(update_fields=[
        "estado",
        "fecha_aprobacion",
        "fecha_observacion",
        "observacion_rechazo",
    ])

    messages.success(request, "✅ Proyecto aprobado. Almacén ya puede despachar materiales.")
    return redirect("tecnico_dashboard")


@login_required
def proyecto_observar_tecnico(request, proyecto_id):
    if request.method != "POST":
        return redirect("proyecto_detail", pk=proyecto_id)

    proyecto = get_object_or_404(Proyecto, id=proyecto_id)
    motivo = request.POST.get("motivo", "").strip()

    if request.user != proyecto.responsable and not request.user.is_superuser:
        messages.error(request, "No tienes permiso para observar este proyecto.")
        return redirect("proyecto_detail", pk=proyecto.id)

    if not proyecto.puede_aprobar_pex:
        messages.error(request, "Solo puedes observar proyectos en revisión técnica.")
        return redirect("proyecto_detail", pk=proyecto.id)

    if not motivo:
        messages.error(request, "Debes indicar el motivo de la observación.")
        return redirect("proyecto_detail", pk=proyecto.id)

    proyecto.estado = EstadoProyecto.OBSERVADO
    proyecto.observacion_rechazo = motivo
    proyecto.fecha_observacion = timezone.now()
    proyecto.save(update_fields=[
        "estado",
        "observacion_rechazo",
        "fecha_observacion",
    ])

    messages.warning(request, "Proyecto observado y devuelto al diseñador para corrección.")
    return redirect("tecnico_dashboard")

@login_required
def proyecto_asignar_cuadrilla(request, proyecto_id):
    proyecto = get_object_or_404(
        Proyecto.objects.select_related("sede", "creado_por", "responsable"),
        id=proyecto_id,
    )

    puede_asignar = (
        request.user.is_superuser
        or request.user == proyecto.responsable
        or getattr(getattr(request.user, "profile", None), "rol", None) in ["ADMIN", "JEFA"]
    )

    if not puede_asignar:
        messages.error(request, "No tienes permisos para asignar materiales de este proyecto.")
        return redirect("proyecto_detail", pk=proyecto.id)

    if proyecto.estado not in [EstadoProyecto.APROBADO, EstadoProyecto.EN_PROCESO]:
        messages.warning(request, "Solo puedes asignar materiales cuando el proyecto esté aprobado o en ejecución.")
        return redirect("proyecto_detail", pk=proyecto.id)

    materiales = (
        ProyectoMaterial.objects
        .select_related("producto")
        .filter(proyecto=proyecto)
        .order_by("producto__nombre")
    )

    tecnicos = (
        User.objects
        .filter(
            is_active=True,
            profile__rol=UserProfile.Rol.SOLICITANTE,
        )
        .exclude(id=request.user.id)
        .select_related("profile")
        .order_by("first_name", "last_name", "username")
    )

    asignaciones = (
        AsignacionCuadrilla.objects
        .filter(proyecto=proyecto)
        .select_related("entregado_por", "recibido_por", "producto")
        .prefetch_related("seriales")
        .order_by("-fecha_entrega", "-id")
    )

    for material in materiales:
        asignado_cuadrilla = AsignacionCuadrilla.objects.filter(
            proyecto=proyecto,
            producto=material.producto,
            estado=EstadoTransferenciaCuadrilla.ENTREGADO,
        ).aggregate(total=Sum("cantidad"))["total"] or 0

        material.asignado_cuadrilla = int(asignado_cuadrilla or 0)
        material.disponible_cuadrilla = max(
            int(material.cantidad_entregada or 0) - int(asignado_cuadrilla or 0),
            0,
        )

    if request.method == "POST":
        tecnico_id = request.POST.get("tecnico_id")
        observaciones = (request.POST.get("observaciones") or "").strip()

        tecnico = User.objects.filter(id=tecnico_id, is_active=True).first()

        if not tecnico:
            messages.error(request, "Selecciona un técnico válido.")
            return redirect("proyecto_asignar_cuadrilla", proyecto_id=proyecto.id)

        if tecnico == request.user:
            messages.error(request, "No puedes asignarte materiales a ti mismo en este flujo.")
            return redirect("proyecto_asignar_cuadrilla", proyecto_id=proyecto.id)

        creadas = 0

        try:
            with transaction.atomic():
                for material in materiales:
                    raw_qty = request.POST.get(f"qty_{material.id}", "0")

                    try:
                        qty = int(raw_qty or 0)
                    except ValueError:
                        qty = 0

                    if qty <= 0:
                        continue

                    asignado_cuadrilla = AsignacionCuadrilla.objects.filter(
                        proyecto=proyecto,
                        producto=material.producto,
                        estado=EstadoTransferenciaCuadrilla.ENTREGADO,
                    ).aggregate(total=Sum("cantidad"))["total"] or 0

                    disponible_cuadrilla = int(material.cantidad_entregada or 0) - int(asignado_cuadrilla or 0)

                    if qty > disponible_cuadrilla:
                        messages.error(
                            request,
                            f"No puedes asignar {qty} de {material.producto.nombre}. "
                            f"Disponible para cuadrilla: {disponible_cuadrilla}."
                        )
                        raise ValueError("Cantidad supera disponible para cuadrilla")

                    asignacion = AsignacionCuadrilla.objects.create(
                        proyecto=proyecto,
                        entregado_por=request.user,
                        recibido_por=tecnico,
                        producto=material.producto,
                        cantidad=qty,
                        estado=EstadoTransferenciaCuadrilla.ENTREGADO,
                        observaciones=observaciones,
                        fecha_entrega=timezone.now(),
                    )

                    creadas += 1

                if creadas == 0:
                    messages.warning(request, "Debes ingresar al menos una cantidad mayor a cero.")
                    raise ValueError("Sin cantidades")

        except ValueError:
            return redirect("proyecto_asignar_cuadrilla", proyecto_id=proyecto.id)

        messages.success(request, f"Materiales asignados correctamente a {tecnico.get_full_name() or tecnico.username}.")
        return redirect("proyecto_asignar_cuadrilla", proyecto_id=proyecto.id)

    context = {
        "proyecto": proyecto,
        "materiales": materiales,
        "tecnicos": tecnicos,
        "asignaciones": asignaciones,
    }

    return render(request, "proyectos/asignar_cuadrilla.html", context)