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
    TipoDocumento, EstadoDocumento, ItemSerializado, StockTecnico, MovimientoInventario,
    Producto,
)
# Importamos modelos locales
from .models import (
    Proyecto, ProyectoMaterial, ProyectoAsignacion, EstadoProyecto,
    AsignacionCuadrilla, EstadoTransferenciaCuadrilla, TipoProyecto,
    ProyectoMaterialPendiente,
)
# ✅ IMPORTANTE: Agregamos ProyectoMaterialForm aquí abajo 👇
from .forms import ProyectoForm, ProyectoMaterialForm, ProyectoMaterialPendienteForm

# Importamos modelos del core
from inventario.models import UserProfile

from .utils import render_to_pdf
from django.utils import timezone
from django.http import HttpResponse, JsonResponse


def _despachos_del_proyecto(proyecto):
    """
    Reconstruye, a partir de los documentos SAL reales, qué sede despachó
    qué cantidad y quién retiró físicamente el material. Un proyecto/avería
    puede recibir despachos de más de una sede (ej. Jauja + Huancayo).
    """
    return (
        DocumentoInventario.objects
        .filter(tipo=TipoDocumento.SAL, referencia=proyecto.codigo)
        .select_related("sede", "retirado_por", "responsable")
        .prefetch_related("items__producto")
        .order_by("fecha")
    )


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

            # Generación de Código (prefijo distinto según tipo: OBRA / AVE)
            prefijo = "AVE" if proyecto.tipo == TipoProyecto.AVERIA else "OBRA"
            year = datetime.date.today().year
            ultimo_proyecto = Proyecto.objects.filter(codigo__startswith=f"{prefijo}-{year}").order_by('id').last()

            if ultimo_proyecto:
                try:
                    correlativo = int(ultimo_proyecto.codigo.split('-')[-1]) + 1
                except ValueError:
                    correlativo = 1
            else:
                correlativo = 1

            proyecto.codigo = f"{prefijo}-{year}-{correlativo:04d}"
            proyecto.save()
            
            messages.success(request, f'Proyecto "{proyecto.nombre}" creado. Agrega los materiales.')
            return redirect('proyecto_materiales', proyecto_id=proyecto.id)
        else:
            messages.error(request, 'Error en el formulario.')
    else:
        form = ProyectoForm()

    return render(request, 'proyectos/proyecto_form.html', {'form': form})


@login_required
def proyecto_cambiar_tipo(request, proyecto_id):
    """
    Permite corregir la clasificación de un proyecto ya creado
    (ej. se creó como "Proyecto" pero en realidad es una Avería).
    """
    proyecto = get_object_or_404(Proyecto, id=proyecto_id)

    if request.user != proyecto.creado_por and not request.user.is_superuser:
        messages.error(request, "No tienes permiso para cambiar el tipo de este proyecto.")
        return redirect("proyecto_detail", pk=proyecto.id)

    if request.method != "POST":
        return redirect("proyecto_detail", pk=proyecto.id)

    nuevo_tipo = (request.POST.get("tipo") or "").strip().upper()

    if nuevo_tipo not in TipoProyecto.values:
        messages.error(request, "Tipo inválido.")
        return redirect("proyecto_detail", pk=proyecto.id)

    proyecto.tipo = nuevo_tipo
    proyecto.save(update_fields=["tipo", "actualizado_en"])

    messages.success(request, f"Clasificación actualizada a: {proyecto.get_tipo_display()}.")
    return redirect("proyecto_detail", pk=proyecto.id)


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

        if request.POST.get("accion") == "dejar_pendiente":
            pendiente_form = ProyectoMaterialPendienteForm(request.POST)

            if pendiente_form.is_valid():
                pendiente = pendiente_form.save(commit=False)
                pendiente.proyecto = proyecto
                pendiente.creado_por = request.user
                pendiente.save()
                messages.success(
                    request,
                    f"Dejado pendiente: {pendiente.nombre_solicitado}. "
                    f"Cuando almacén lo registre en el catálogo podrás vincularlo."
                )
            else:
                messages.error(request, "Revisa el material pendiente: falta el nombre o la cantidad.")

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
    pendientes = proyecto.materiales_pendientes.filter(resuelto=False).select_related("creado_por")
    pendiente_form = ProyectoMaterialPendienteForm()
    productos_catalogo = Producto.objects.filter(activo=True).order_by("nombre")

    return render(request, "proyectos/materiales_form.html", {
        "proyecto": proyecto,
        "form": form,
        "materiales": materiales,
        "pendientes": pendientes,
        "pendiente_form": pendiente_form,
        "productos_catalogo": productos_catalogo,
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


@login_required
@transaction.atomic
def proyecto_material_pendiente_vincular(request, pendiente_id):
    """
    Convierte un material 'dejado pendiente' en un ProyectoMaterial real,
    una vez que Almacén ya registró el producto en el catálogo.
    """
    pendiente = get_object_or_404(
        ProyectoMaterialPendiente.objects.select_related("proyecto"),
        id=pendiente_id,
    )
    proyecto = pendiente.proyecto

    if request.user != proyecto.creado_por and not request.user.is_superuser:
        messages.error(request, "No tienes permiso para vincular este material.")
        return redirect("proyecto_materiales", proyecto_id=proyecto.id)

    if pendiente.resuelto:
        messages.warning(request, "Este material pendiente ya fue vinculado.")
        return redirect("proyecto_materiales", proyecto_id=proyecto.id)

    if not proyecto.puede_editar_materiales:
        messages.error(request, "No puedes editar materiales en este estado.")
        return redirect("proyecto_materiales", proyecto_id=proyecto.id)

    if request.method != "POST":
        return redirect("proyecto_materiales", proyecto_id=proyecto.id)

    producto_id = request.POST.get("producto_id")
    producto = get_object_or_404(Producto, id=producto_id, activo=True)

    material, creado = ProyectoMaterial.objects.get_or_create(
        proyecto=proyecto,
        producto=producto,
        defaults={
            "cantidad_planificada": pendiente.cantidad_estimada,
            "observacion_diseno": pendiente.nota,
        },
    )

    if not creado:
        material.cantidad_planificada += pendiente.cantidad_estimada
        material.save(update_fields=["cantidad_planificada", "actualizado_en"])

    pendiente.resuelto = True
    pendiente.producto_vinculado = producto
    pendiente.material_resultante = material
    pendiente.resuelto_en = timezone.now()
    pendiente.save(update_fields=[
        "resuelto", "producto_vinculado", "material_resultante", "resuelto_en", "actualizado_en",
    ])

    messages.success(
        request,
        f"'{pendiente.nombre_solicitado}' vinculado a {producto.nombre}. "
        f"Cantidad agregada a la lista de materiales."
    )
    return redirect("proyecto_materiales", proyecto_id=proyecto.id)


@login_required
def proyecto_material_pendiente_eliminar(request, pendiente_id):
    pendiente = get_object_or_404(ProyectoMaterialPendiente.objects.select_related("proyecto"), id=pendiente_id)
    proyecto = pendiente.proyecto

    if request.user != proyecto.creado_por and not request.user.is_superuser:
        messages.error(request, "No tienes permiso para eliminar este pendiente.")
        return redirect("proyecto_materiales", proyecto_id=proyecto.id)

    pendiente.delete()
    messages.success(request, "Material pendiente eliminado.")
    return redirect("proyecto_materiales", proyecto_id=proyecto.id)

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
    pendientes = proyecto.materiales_pendientes.filter(resuelto=False)

    costo_total = sum(m.costo_total_real for m in materiales)

    despachos = _despachos_del_proyecto(proyecto)

    return render(request, 'proyectos/detalle.html', {
        'proyecto': proyecto,
        'materiales': materiales,
        'tecnicos': tecnicos,
        'costo_total': costo_total,
        'pendientes': pendientes,
        'despachos': despachos,
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

    # Un almacén puede apoyar despachos de obras de otras sedes.
    # Por eso mostramos proyectos aprobados/en proceso de todas las sedes.
    proyectos = (
        Proyecto.objects
        .filter(
            estado__in=[
                EstadoProyecto.APROBADO,
                EstadoProyecto.EN_PROCESO,
            ]
        )
        .select_related("sede", "responsable", "creado_por")
        .order_by("-creado_en")
    )

    return render(request, 'proyectos/almacen_proyectos_list.html', {
        'proyectos': proyectos,
        'sede_almacen': sede_almacen,
    })


@login_required
def almacen_proyecto_detalle(request, proyecto_id):
    profile = getattr(request.user, 'profile', None)

    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return redirect('home')

    sede_almacen = profile.get_sede_operativa()

    proyecto = get_object_or_404(
        Proyecto.objects.select_related("sede", "responsable", "creado_por"),
        id=proyecto_id,
    )

    materiales = proyecto.materiales.select_related('producto').all()

    # El stock mostrado debe ser el stock de la sede del almacenero actual,
    # no necesariamente la sede principal del proyecto.
    for item in materiales:
        stock_item = Stock.objects.filter(
            producto=item.producto,
            sede=sede_almacen,
        ).first()

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
        'progreso': int(progreso),
        'sede_almacen': sede_almacen,
    })


@login_required
def almacen_generar_salida(request, proyecto_id):
    profile = getattr(request.user, 'profile', None)

    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return redirect('home')

    sede_despacho = profile.get_sede_operativa()

    proyecto = get_object_or_404(
        Proyecto.objects.select_related("sede", "responsable", "creado_por"),
        id=proyecto_id,
    )

    # Solo se puede despachar si la obra ya fue aprobada o ya está en ejecución.
    if proyecto.estado not in [EstadoProyecto.APROBADO, EstadoProyecto.EN_PROCESO]:
        messages.error(request, "El proyecto no está aprobado para despacho.")
        return redirect('almacen_proyectos_list')

    materiales = proyecto.materiales.select_related('producto').all()
    items_pendientes = []

    # Calculamos pendiente general del proyecto,
    # pero stock disponible de la sede que está despachando.
    for m in materiales:
        pendiente = int(m.cantidad_planificada or 0) - int(m.cantidad_entregada or 0)

        if pendiente > 0:
            stock_obj = Stock.objects.filter(
                producto=m.producto,
                sede=sede_despacho,
            ).first()

            stock_actual = int(stock_obj.cantidad or 0) if stock_obj else 0
            sugerido = min(pendiente, stock_actual)

            m.stock_temp = stock_actual
            m.pendiente_temp = pendiente
            m.sugerido = sugerido

            items_pendientes.append(m)

    if not items_pendientes:
        messages.success(request, "Todo entregado.")
        return redirect('almacen_proyecto_detalle', proyecto_id=proyecto.id)

    tecnicos = (
        User.objects
        .filter(is_active=True, profile__rol=UserProfile.Rol.SOLICITANTE)
        .select_related("profile")
        .order_by("first_name", "last_name", "username")
    )

    if request.method == 'POST':
        try:
            with transaction.atomic():
                notas_usuario = request.POST.get('notas', '').strip()
                retirado_por_id = (request.POST.get('retirado_por_id') or '').strip()

                # Por defecto, quien retira es el responsable del proyecto.
                # Si otro técnico está sacando el material a su nombre
                # (frecuente al cargar mochila), se elige aquí para que
                # quede reflejado en el vale y en los reportes.
                retirado_por = proyecto.responsable
                if retirado_por_id:
                    retirado_por = get_object_or_404(User, id=retirado_por_id, is_active=True)

                observaciones = f"Salida para obra {proyecto.codigo} | Sede del proyecto: {proyecto.sede.nombre}"

                if notas_usuario:
                    observaciones += f" | Nota: {notas_usuario}"

                doc = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.SAL,
                    estado=EstadoDocumento.BORRADOR,

                    # Sede que realmente entrega y de donde se descuenta stock.
                    sede=sede_despacho,

                    # OJO:
                    # No usar sede_destino aquí, porque el sistema lo interpreta como transferencia entre sedes.
                    # Esta salida es para una obra/proyecto, no una transferencia.
                    responsable=request.user,
                    solicitante=proyecto.responsable,
                    retirado_por=retirado_por,
                    referencia=proyecto.codigo,
                    observaciones=observaciones,
                )

                hubo_movimiento = False

                for m in items_pendientes:
                    try:
                        qty = int(request.POST.get(f'input_{m.id}', 0) or 0)
                    except ValueError:
                        qty = 0

                    if qty <= 0:
                        continue

                    if qty > m.pendiente_temp:
                        raise ValueError(
                            f"No puedes despachar más de lo pendiente en {m.producto.nombre}. "
                            f"Pendiente: {m.pendiente_temp}."
                        )

                    if qty > m.stock_temp:
                        raise ValueError(
                            f"Stock insuficiente en {sede_despacho.nombre}: {m.producto.nombre}. "
                            f"Disponible: {m.stock_temp}."
                        )

                    # SERIALIZADOS: validar que los equipos físicos existan
                    # y pertenezcan a la sede que está despachando.
                    if m.producto.es_serializado:
                        seriales_ingresados = request.POST.getlist(f'macs_{m.id}')
                        seriales_ingresados = [
                            s.strip().upper()
                            for s in seriales_ingresados
                            if s.strip()
                        ]

                        if len(seriales_ingresados) != qty:
                            raise ValueError(
                                f"Debes ingresar exactamente {qty} MACs/Series para {m.producto.nombre}."
                            )

                        for serial in seriales_ingresados:
                            item_fisico = ItemSerializado.objects.filter(
                                Q(serial__iexact=serial)
                                | Q(mac_address__iexact=serial)
                                | Q(serial_secundario__iexact=serial)
                                | Q(codigo_trazabilidad__iexact=serial),
                                producto=m.producto,
                                estado=ItemSerializado.Estado.EN_ALMACEN,
                                ubicacion__sede=sede_despacho,
                            ).first()

                            if not item_fisico:
                                raise ValueError(
                                    f"El equipo '{serial}' no está disponible en {sede_despacho.nombre} "
                                    f"o no existe para {m.producto.nombre}."
                                )

                            item_fisico.estado = ItemSerializado.Estado.ASIGNADO
                            item_fisico.asignado_a = proyecto.responsable
                            item_fisico.ubicacion = None
                            item_fisico.save(update_fields=[
                                "estado",
                                "asignado_a",
                                "ubicacion",
                            ])

                    DocumentoItem.objects.create(
                        documento=doc,
                        producto=m.producto,
                        cantidad=qty,
                    )

                    m.cantidad_entregada += qty
                    m.save(update_fields=["cantidad_entregada", "actualizado_en"])

                    hubo_movimiento = True

                if not hubo_movimiento:
                    messages.warning(request, "No seleccionaste cantidades.")
                    doc.delete()
                    return redirect('almacen_generar_salida', proyecto_id=proyecto.id)

                # doc.confirmar() descuenta stock de doc.sede.
                # Como doc.sede = sede_despacho, baja de la sede correcta.
                doc.confirmar()

                if proyecto.estado == EstadoProyecto.APROBADO:
                    proyecto.estado = EstadoProyecto.EN_PROCESO
                    proyecto.save(update_fields=["estado", "actualizado_en"])

                messages.success(
                    request,
                    f"Despacho {doc.numero} realizado desde {sede_despacho.nombre}."
                )
                return redirect('almacen_proyecto_detalle', proyecto_id=proyecto.id)

        except Exception as e:
            messages.error(request, f"Error: {str(e)}")

    return render(request, 'proyectos/almacen_generar_salida.html', {
        'proyecto': proyecto,
        'items': items_pendientes,
        'sede_despacho': sede_despacho,
        'tecnicos': tecnicos,
    })

@login_required
def ajax_buscar_equipo_proyecto(request):
    profile = getattr(request.user, "profile", None)

    if not profile or profile.rol != UserProfile.Rol.ALMACEN:
        return JsonResponse({
            "ok": False,
            "error": "No tienes permisos para validar equipos."
        }, status=403)

    sede_despacho = profile.get_sede_operativa()

    producto_id = request.GET.get("producto_id")
    codigo = (request.GET.get("codigo") or "").strip().upper()

    if not producto_id or not codigo:
        return JsonResponse({
            "ok": False,
            "error": "Ingresa un código, SN, MAC o D-SN."
        }, status=400)

    equipo = (
        ItemSerializado.objects
        .filter(
            Q(serial__iexact=codigo)
            | Q(mac_address__iexact=codigo)
            | Q(serial_secundario__iexact=codigo)
            | Q(codigo_trazabilidad__iexact=codigo),
            producto_id=producto_id,
            estado=ItemSerializado.Estado.EN_ALMACEN,
            ubicacion__sede=sede_despacho,
        )
        .select_related("producto", "ubicacion", "ubicacion__sede")
        .first()
    )

    if not equipo:
        return JsonResponse({
            "ok": False,
            "error": f"El equipo '{codigo}' no está disponible en {sede_despacho.nombre}, ya fue asignado o no existe."
        }, status=404)

    return JsonResponse({
        "ok": True,
        "equipo": {
            "id": equipo.id,
            "producto": equipo.producto.nombre,
            "serial": equipo.serial or "",
            "mac": equipo.mac_address or "",
            "dsn": equipo.serial_secundario or "",
            "codigo": equipo.codigo_trazabilidad or "",
            "sede": sede_despacho.nombre,
        }
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
        'despachos': _despachos_del_proyecto(proyecto),
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
        'despachos': _despachos_del_proyecto(proyecto),
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