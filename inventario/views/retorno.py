"""Preparación y revisión de devoluciones de un almacén secundario a central."""

from django import forms
from django.contrib import messages
from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import F, Q
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods, require_POST

from inventario.models import (
    DocumentoInventario, EstadoDocumento, ItemSerializado, Producto,
    TipoDocumento, TipoRequerimiento, UserProfile,
)
from inventario.permissions import role_required
from inventario.services.retorno_service import (
    confirmar_retorno, crear_borrador_retorno, guardar_borrador_retorno,
    huella_retorno, obtener_contexto_retorno,
)


class ProductoRetornoField(forms.ModelChoiceField):
    def label_from_instance(self, producto):
        return (
            f"{producto.codigo_interno} — {producto.nombre} "
            f"({producto.unidad}; disponible: {producto.disponible})"
        )


class SerialesRetornoWidget(forms.SelectMultiple):
    def create_option(self, name, value, label, selected, index, subindex=None, attrs=None):
        option = super().create_option(name, value, label, selected, index, subindex, attrs)
        if hasattr(value, "instance"):
            option["attrs"]["data-producto"] = value.instance.producto_id
        return option


class SerialesRetornoField(forms.ModelMultipleChoiceField):
    def label_from_instance(self, equipo):
        codigo = f" · Código {equipo.codigo_trazabilidad}" if equipo.codigo_trazabilidad else ""
        return f"{equipo.producto.nombre} · Serie {equipo.serial}{codigo}"


class RetornoForm(forms.Form):
    documento_id = forms.IntegerField(min_value=1, widget=forms.HiddenInput)
    producto = ProductoRetornoField(queryset=Producto.objects.none(), label="Producto")
    cantidad = forms.IntegerField(min_value=1, label="Cantidad", widget=forms.NumberInput(attrs={"step": "1"}))
    motivo = forms.CharField(
        label="Motivo de la devolución", max_length=2000,
        widget=forms.Textarea(attrs={"rows": 3, "placeholder": "Indica por qué retorna este material a central."}),
    )
    seriales = SerialesRetornoField(
        queryset=ItemSerializado.objects.none(), required=False,
        label="Equipos que se enviarán", widget=SerialesRetornoWidget(attrs={"size": 7}),
    )

    def __init__(self, *args, sede, **kwargs):
        super().__init__(*args, **kwargs)
        try:
            seleccionado = int(self.data.get("producto") if self.is_bound else self.initial.get("producto"))
        except (TypeError, ValueError):
            seleccionado = None
        self.fields["producto"].queryset = Producto.objects.filter(
            Q(stocks__cantidad__gt=0) | Q(pk=seleccionado),
            activo=True, stocks__sede=sede,
        ).annotate(disponible=F("stocks__cantidad")).order_by("nombre", "pk")
        self.fields["seriales"].queryset = ItemSerializado.objects.filter(
            producto_id=seleccionado,
            ubicacion__sede=sede, estado=ItemSerializado.Estado.EN_ALMACEN,
            asignado_a__isnull=True, proyecto__isnull=True,
        ).select_related("producto").order_by("producto__nombre", "serial")


def _documento_propio(user, documento_id, contexto):
    try:
        documento_id = int(documento_id)
    except (TypeError, ValueError):
        raise Http404("No se encontró el borrador de devolución.")
    return get_object_or_404(
        DocumentoInventario.objects.select_related("sede", "sede_destino"),
        pk=documento_id, tipo=TipoDocumento.SAL,
        tipo_requerimiento=TipoRequerimiento.RETORNO_CENTRAL,
        responsable=user, sede=contexto["sede_origen"], sede_destino=contexto["sede_destino"],
    )


def _preparacion(request, contexto, documento=None, form=None, error=None):
    productos = []
    if form is not None:
        productos = list(form.fields["producto"].queryset.values(
            "id", "unidad", "es_serializado", "disponible",
        ))
    return render(request, "inventario/retorno_central_form.html", {
        **contexto, "documento": documento, "borrador": documento,
        "form": form, "error_configuracion": error, "productos_retorno": productos,
    })


@role_required(UserProfile.Rol.ALMACEN, allow_superuser=False)
@require_http_methods(["GET", "POST"])
def retorno_central_crear(request):
    try:
        contexto = obtener_contexto_retorno(request.user)
    except ValidationError as exc:
        return _preparacion(request, {}, error=" ".join(exc.messages))

    if request.method == "POST":
        # Nunca crear otro documento al repetir un POST: siempre se recibe el ID
        # persistente que se entregó al abrir la preparación.
        documento = _documento_propio(request.user, request.POST.get("documento_id"), contexto)
        form = RetornoForm(request.POST, sede=contexto["sede_origen"])
        if form.is_valid():
            try:
                documento = guardar_borrador_retorno(
                    user=request.user, documento_id=documento.pk,
                    observaciones=form.cleaned_data["motivo"],
                    items=[{
                        "producto_id": form.cleaned_data["producto"].pk,
                        "cantidad": form.cleaned_data["cantidad"],
                        "seriales_ids": [equipo.pk for equipo in form.cleaned_data["seriales"]],
                    }],
                )
            except ValidationError as exc:
                form.add_error(None, exc)
            else:
                return redirect("retorno_central_detalle", sal_id=documento.pk)
        return _preparacion(request, contexto, documento, form)

    # El borrador vacío estable no mueve stock. Se reutiliza bajo bloqueo por
    # operador; todos los siguientes guardados y confirmaciones usan ese mismo ID.
    try:
        documento = crear_borrador_retorno(user=request.user)
    except ValidationError as exc:
        return _preparacion(request, contexto, error=" ".join(exc.messages))
    items = list(documento.items.prefetch_related("seriales_seleccionados"))
    if len(items) > 1:
        # El servicio admite varios productos. Esta preparación registra uno por
        # devolución y nunca debe borrar otras líneas de un documento existente.
        return redirect("retorno_central_detalle", sal_id=documento.pk)
    initial = {"documento_id": documento.pk, "motivo": documento.observaciones}
    if items:
        initial.update({
            "producto": items[0].producto_id, "cantidad": items[0].cantidad,
            "seriales": [s.item_serializado_id for s in items[0].seriales_seleccionados.all()],
        })
    form = RetornoForm(initial=initial, sede=contexto["sede_origen"])
    return _preparacion(request, contexto, documento, form)


@transaction.atomic
def _revision(request, documento, contexto, error=None):
    # Guardar y confirmar bloquean esta misma fila: el resumen y su huella
    # pertenecen a la misma versión, incluso con dos pestañas abiertas.
    documento = DocumentoInventario.objects.select_for_update().get(pk=documento.pk)
    items = list(documento.items.select_related("producto").prefetch_related(
        "seriales_seleccionados__item_serializado",
    ))
    return render(request, "inventario/retorno_central_detalle.html", {
        **contexto, "documento": documento, "items": items,
        "revision": huella_retorno(documento, items=items), "error_confirmacion": error,
        "puede_editar": documento.estado == EstadoDocumento.BORRADOR and len(items) <= 1,
    })


@role_required(UserProfile.Rol.ALMACEN, allow_superuser=False)
@require_http_methods(["GET"])
def retorno_central_detalle(request, sal_id):
    try:
        contexto = obtener_contexto_retorno(request.user)
    except ValidationError as exc:
        return _preparacion(request, {}, error=" ".join(exc.messages))
    documento = _documento_propio(request.user, sal_id, contexto)
    return _revision(request, documento, contexto)


@role_required(UserProfile.Rol.ALMACEN, allow_superuser=False)
@require_POST
def retorno_central_confirmar(request, sal_id):
    try:
        contexto = obtener_contexto_retorno(request.user)
    except ValidationError as exc:
        return _preparacion(request, {}, error=" ".join(exc.messages))
    documento = _documento_propio(request.user, sal_id, contexto)
    if request.POST.get("acepto") != "1":
        return _revision(request, documento, contexto, "Confirma que revisaste el material y la cantidad que saldrá de tu sede.")
    try:
        documento = confirmar_retorno(
            user=request.user, documento_id=documento.pk,
            revision=request.POST.get("revision", ""),
        )
    except ValidationError as exc:
        documento.refresh_from_db()
        return _revision(request, documento, contexto, " ".join(exc.messages))
    messages.success(request, f"Devolución enviada con {documento.numero}. Central debe confirmar su recepción.")
    return redirect("retorno_central_detalle", sal_id=documento.pk)


@role_required(UserProfile.Rol.ALMACEN, allow_superuser=False)
@require_http_methods(["GET"])
def retorno_central_equipos(request):
    """Opciones de equipos exclusivamente para el producto elegido en esta sede."""
    try:
        contexto = obtener_contexto_retorno(request.user)
        producto_id = int(request.GET.get("producto", ""))
    except (TypeError, ValueError, ValidationError):
        return JsonResponse({"error": "No se pudo consultar el producto."}, status=400)
    producto = get_object_or_404(
        Producto, pk=producto_id, activo=True,
        stocks__sede=contexto["sede_origen"], stocks__cantidad__gt=0,
    )
    if not producto.es_serializado:
        return JsonResponse({"equipos": []})
    equipos = ItemSerializado.objects.filter(
        producto=producto, ubicacion__sede=contexto["sede_origen"],
        estado=ItemSerializado.Estado.EN_ALMACEN,
        asignado_a__isnull=True, proyecto__isnull=True,
    ).order_by("serial")
    return JsonResponse({"equipos": [
        {"id": equipo.pk, "texto": f"Serie {equipo.serial}" + (
            f" · Código {equipo.codigo_trazabilidad}" if equipo.codigo_trazabilidad else ""
        )} for equipo in equipos
    ]})
