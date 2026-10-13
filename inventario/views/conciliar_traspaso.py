"""Identificación explícita de transferencias antiguas, sin mover existencias."""
from django.contrib import messages
from django.core.exceptions import PermissionDenied, ValidationError
from django.db import transaction
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

from inventario.models import (
    CorreccionSeriales, DocumentoInventario, DocumentoItemSerializado,
    ItemSerializado, UserProfile,
)
from inventario.permissions import role_required
from inventario.services.seriales_service import ids_unicos, registrar_seriales


def validar_destino(sal, usuario):
    sede = usuario.profile.get_sede_operativa()
    if not sede or sal.sede_destino_id != sede.id:
        raise PermissionDenied('Esta transferencia no corresponde a tu sede receptora.')
    if sal.estado != 'CONFIRMADO' or sal.sede_id == sede.id or not sal.solicitante_id:
        raise ValidationError('Se requiere una transferencia confirmada con destinatario.')
    if sal.recibido or DocumentoInventario.objects.filter(tipo='ING', origen=sal).exists():
        raise ValidationError('Esta salida ya tiene una recepción registrada. Actualiza la página.')


def candidatos(sal, linea):
    # No adjudicar un equipo que ya está identificado en otra salida pendiente.
    comprometidos = DocumentoItemSerializado.objects.filter(
        documento_item__documento__tipo='SAL',
        documento_item__documento__estado='CONFIRMADO',
        documento_item__documento__recibido=False,
        documento_item__documento__sede_destino__isnull=False,
    ).exclude(documento_item__documento=sal).values('item_serializado_id')
    return ItemSerializado.objects.filter(
        producto=linea.producto, asignado_a_id=sal.solicitante_id,
        estado='ASIGNADO', proyecto__isnull=True, ubicacion__isnull=True,
    ).filter(Q(sede_asignacion=sal.sede) | Q(sede_asignacion__isnull=True)).exclude(
        pk__in=comprometidos,
    ).order_by('id')


@require_http_methods(['GET', 'POST'])
@role_required(UserProfile.Rol.ALMACEN, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)
def conciliar_traspaso(request, sal_id):
    sal = get_object_or_404(DocumentoInventario, pk=sal_id, tipo='SAL')
    try:
        validar_destino(sal, request.user)
        if request.method == 'POST':
            with transaction.atomic():
                # Mismo orden de bloqueo que la recepción: documento, luego equipos.
                sal = DocumentoInventario.objects.select_for_update().get(pk=sal.pk)
                validar_destino(sal, request.user)
                if request.POST.get('confirmacion_fisica') != 'si':
                    raise ValidationError('Confirma que los códigos seleccionados coinciden con los equipos recibidos.')
                detalle = []
                for linea in sal.items.select_related('producto').order_by('id'):
                    if not linea.producto.es_serializado:
                        continue
                    vinculados = list(linea.seriales_seleccionados.values_list('item_serializado_id', flat=True))
                    if vinculados:
                        if len(vinculados) != linea.cantidad:
                            raise ValidationError('La salida tiene una identificación parcial; requiere revisión administrativa.')
                        continue
                    ids = ids_unicos(request.POST.getlist(f'equipos_{linea.pk}'))
                    if len(ids) != linea.cantidad:
                        raise ValidationError(f'{linea.producto.nombre}: selecciona exactamente {linea.cantidad} equipos.')
                    # Bloquear primero, volver a consultar compromisos después de cualquier espera.
                    equipos = list(ItemSerializado.objects.select_for_update().filter(pk__in=ids).order_by('id'))
                    validos = set(candidatos(sal, linea).filter(pk__in=ids).values_list('id', flat=True))
                    if validos != set(ids) or len(equipos) != len(ids):
                        raise ValidationError('La selección cambió o incluye equipos de otra salida, producto o destinatario.')
                    detalle.append({'linea': linea.pk, 'equipos': [
                        {'id': e.pk, 'codigo': e.codigo_trazabilidad, 'serial': e.serial,
                         'sede_anterior': e.sede_asignacion_id, 'sede_origen': sal.sede_id}
                        for e in equipos]})
                    for e in equipos:
                        e.sede_asignacion = sal.sede
                        e.save(update_fields=['sede_asignacion', 'actualizado_en'])
                    registrar_seriales(linea, equipos)
                if detalle:
                    CorreccionSeriales.objects.create(
                        clave=f'traspaso-{sal.pk}-identificacion', usuario=request.user,
                        detalle={'motivo': 'Identificación confirmada físicamente antes de recepción',
                                 'salida': sal.numero, 'lineas': detalle, 'stock_modificado': False},
                    )
            messages.success(request, 'Equipos identificados. Revisa los números en la salida y confirma su ingreso.')
            return redirect('sal_detail', sal_id=sal.pk)
    except ValidationError as e:
        messages.error(request, ' '.join(e.messages))
        if sal.recibido or DocumentoInventario.objects.filter(tipo='ING', origen=sal).exists():
            return redirect('sal_detail', sal_id=sal.pk)
    lineas = []
    for linea in sal.items.select_related('producto').order_by('id'):
        if linea.producto.es_serializado:
            linea.identificados = list(linea.seriales_seleccionados.select_related('item_serializado'))
            linea.candidatos = list(candidatos(sal, linea)) if not linea.identificados else []
            lineas.append(linea)
    return render(request, 'inventario/conciliar_traspaso.html', {'sal': sal, 'lineas': lineas})
