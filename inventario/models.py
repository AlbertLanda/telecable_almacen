from __future__ import annotations

import re
from decimal import Decimal

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models, transaction
from django.db.models import Max
from django.utils import timezone

User = get_user_model()

# ============================================================
# Helpers de códigos
# ============================================================
INTERNAL_PREFIX = "TC-ALM-"
INTERNAL_RE = re.compile(r"^TC-ALM-(\d{6})$")


# ============================================================
# Base
# ============================================================
class TimeStampedModel(models.Model):
    creado_en = models.DateTimeField(auto_now_add=True)
    actualizado_en = models.DateTimeField(auto_now=True)

    class Meta:
        abstract = True


# ============================================================
# Maestros: Sede / Ubicacion / Categoria / Producto
# ============================================================
class Sede(TimeStampedModel):
    CENTRAL = "CENTRAL"
    SECUNDARIO = "SECUNDARIO"
    TIPO_CHOICES = [(CENTRAL, "Central"), (SECUNDARIO, "Secundario")]

    nombre = models.CharField(max_length=80, unique=True)
    descripcion = models.CharField(max_length=200, blank=True, default="")
    tipo = models.CharField(max_length=12, choices=TIPO_CHOICES, default=SECUNDARIO)
    activo = models.BooleanField(default=True)

    class Meta:
        verbose_name = "Sede"
        verbose_name_plural = "Sedes"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre

    def clean(self):
        if self.tipo == self.CENTRAL:
            qs = Sede.objects.filter(tipo=self.CENTRAL)
            if self.pk:
                qs = qs.exclude(pk=self.pk)
            if qs.exists():
                raise ValidationError({"tipo": "Solo puede existir una sede CENTRAL (ej. Jauja)."})


class Categoria(TimeStampedModel):
    nombre = models.CharField(max_length=100, unique=True)

    class Meta:
        verbose_name = "Categoría"
        verbose_name_plural = "Categorías"
        ordering = ["nombre"]

    def __str__(self):
        return self.nombre


class Ubicacion(TimeStampedModel):
    nombre = models.CharField(max_length=100)
    descripcion = models.CharField(max_length=200, blank=True, default="")
    sede = models.ForeignKey(Sede, on_delete=models.PROTECT, related_name="ubicaciones")

    class Meta:
        verbose_name = "Ubicación"
        verbose_name_plural = "Ubicaciones"
        ordering = ["sede__nombre", "nombre"]
        constraints = [
            models.UniqueConstraint(fields=["sede", "nombre"], name="uq_ubicacion_sede_nombre"),
        ]
        indexes = [
            models.Index(fields=["sede", "nombre"]),
        ]

    def __str__(self):
        return f"{self.sede.nombre} - {self.nombre}"


class Producto(TimeStampedModel):
    nombre = models.CharField(max_length=200)
    categoria = models.ForeignKey(
        Categoria, on_delete=models.PROTECT, related_name="productos", null=True, blank=True
    )
    codigo_interno = models.CharField(max_length=20, unique=True, blank=True)
    barcode = models.CharField(max_length=32, unique=True, null=True, blank=True)
    unidad = models.CharField(max_length=20, default="UND")

    costo_unitario = models.DecimalField(
        max_digits=12,
        decimal_places=2,
        default=Decimal("0.00"),
        validators=[MinValueValidator(Decimal("0.00"))],
        help_text="Costo estándar por unidad (según 'unidad').",
    )

    stock_minimo = models.PositiveIntegerField(default=0)
    activo = models.BooleanField(default=True)
    es_serializado = models.BooleanField(
        default=False,
        help_text="True para equipos con serial (ONU/Router). False para consumibles.",
    )

    class Meta:
        verbose_name = "Producto"
        verbose_name_plural = "Productos"
        ordering = ["nombre"]
        indexes = [
            models.Index(fields=["nombre"]),
            models.Index(fields=["codigo_interno"]),
            models.Index(fields=["barcode"]),
        ]

    def __str__(self):
        ci = self.codigo_interno or "SIN-COD"
        return f"{self.nombre} ({ci})"

    @property
    def unidad_medida(self):
        return self.unidad

    def clean(self):
        if self.barcode:
            b = self.barcode.strip().upper()
            if not re.match(r"^[A-Z0-9\-]{4,32}$", b):
                raise ValidationError({"barcode": "Código alfanumérico sin espacios (4-32)."})
            self.barcode = b

        if self.codigo_interno:
            ci = self.codigo_interno.strip().upper()
            if not INTERNAL_RE.match(ci):
                raise ValidationError({"codigo_interno": "Formato: TC-ALM-000001."})
            self.codigo_interno = ci

    def _next_internal_code(self):
        max_code = Producto.objects.filter(codigo_interno__startswith=INTERNAL_PREFIX).aggregate(
            m=Max("codigo_interno")
        )["m"]

        if not max_code:
            return f"{INTERNAL_PREFIX}000001"

        m = INTERNAL_RE.match(max_code)
        n = int(m.group(1)) + 1 if m else 1
        return f"{INTERNAL_PREFIX}{n:06d}"

    def save(self, *args, **kwargs):
        if not self.codigo_interno:
            with transaction.atomic():
                self.codigo_interno = self._next_internal_code()
                super().save(*args, **kwargs)
        else:
            super().save(*args, **kwargs)


class ProductoSedeInfo(TimeStampedModel):
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="info_por_sede")
    sede = models.ForeignKey(Sede, on_delete=models.PROTECT, related_name="productos_info")
    ubicacion_referencial = models.ForeignKey(
        Ubicacion, on_delete=models.SET_NULL, null=True, blank=True, related_name="productos_referenciales"
    )

    class Meta:
        verbose_name = "Info Producto por Sede"
        verbose_name_plural = "Info Productos por Sede"
        constraints = [
            models.UniqueConstraint(fields=["producto", "sede"], name="uq_producto_sede_info"),
        ]

    def clean(self):
        if self.ubicacion_referencial and self.ubicacion_referencial.sede_id != self.sede_id:
            raise ValidationError({"ubicacion_referencial": "La ubicación no pertenece a la sede."})


# ============================================================
# Stock + Kardex
# ============================================================
class Stock(TimeStampedModel):
    producto = models.ForeignKey(Producto, on_delete=models.CASCADE, related_name="stocks")
    sede = models.ForeignKey(Sede, on_delete=models.PROTECT, related_name="stocks")
    cantidad = models.IntegerField(default=0)
    actualizado_en_operacion = models.DateTimeField(default=timezone.now)

    class Meta:
        verbose_name = "Stock"
        verbose_name_plural = "Stocks"
        constraints = [
            models.UniqueConstraint(fields=["producto", "sede"], name="uq_stock_producto_sede"),
        ]
        indexes = [
            models.Index(fields=["producto", "sede"]),
        ]

    def __str__(self):
        return f"{self.producto.codigo_interno} @ {self.sede.nombre} = {self.cantidad}"


class MovimientoInventario(TimeStampedModel):
    TIPO_IN = "IN"
    TIPO_OUT = "OUT"
    TIPO_ADJ = "ADJ"
    TIPOS = [
        (TIPO_IN, "Ingreso"),
        (TIPO_OUT, "Salida"),
        (TIPO_ADJ, "Ajuste"),
    ]

    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="movimientos")
    sede = models.ForeignKey(Sede, on_delete=models.PROTECT, related_name="movimientos")
    ubicacion = models.ForeignKey(
        Ubicacion, on_delete=models.PROTECT, null=True, blank=True, related_name="movimientos"
    )
    
    tipo = models.CharField(max_length=3, choices=TIPOS)
    qty = models.IntegerField(default=0, help_text="Cantidad movida. Positiva en IN/OUT.")
    
    costo_unitario = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    costo_total = models.DecimalField(max_digits=14, decimal_places=2, null=True, blank=True)
    
    referencia = models.CharField(max_length=120, blank=True)
    nota = models.CharField(max_length=250, blank=True)
    
    # ✅ CAMPO NUEVO 1: Usuario (Para saber quién movió el stock)
    usuario = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, blank=True)
    
    # ✅ CAMPO NUEVO 2: Saldo Resultante (Vital para el Kardex: cuánto había después de mover)
    saldo_resultante = models.IntegerField(default=0, help_text="Stock resultante tras el movimiento")

    class Meta:
        verbose_name = "Movimiento de Inventario"
        verbose_name_plural = "Movimientos de Inventario"
        ordering = ["-creado_en"]
        indexes = [
            models.Index(fields=["producto", "sede", "tipo"]),
            models.Index(fields=["creado_en"]),
            models.Index(fields=["referencia"]),
        ]

    def __str__(self):
        ub = self.ubicacion.nombre if self.ubicacion_id else "SIN-UBI"
        return f"{self.get_tipo_display()} {self.qty} - {self.producto.codigo_interno} @ {self.sede.nombre} ({ub})"

    def clean(self):
        if self.tipo in (self.TIPO_IN, self.TIPO_OUT):
            if self.qty <= 0:
                raise ValidationError({"qty": "IN/OUT requieren qty > 0."})
        if self.tipo == self.TIPO_ADJ:
            if self.qty == 0:
                raise ValidationError({"qty": "ADJ requiere qty distinto de 0."})

    def save(self, *args, **kwargs):
        if self.costo_unitario is None:
            self.costo_unitario = self.producto.costo_unitario
        if self.costo_unitario is not None:
            self.costo_total = (Decimal(abs(self.qty)) * Decimal(self.costo_unitario)).quantize(Decimal("0.01"))
        super().save(*args, **kwargs)

    @transaction.atomic
    def aplicar(self):
        """
        Aplica el movimiento al Stock físico y guarda el saldo resultante.
        """
        stock, _ = Stock.objects.select_for_update().get_or_create(
            producto=self.producto, sede=self.sede, defaults={"cantidad": 0}
        )
        
        if self.tipo == self.TIPO_IN:
            stock.cantidad += self.qty
        elif self.tipo == self.TIPO_OUT:
            stock.cantidad -= self.qty
            if stock.cantidad < 0:
                raise ValidationError(f"No hay stock suficiente de {self.producto.nombre}. Faltan {abs(stock.cantidad)}.")
        elif self.tipo == self.TIPO_ADJ:
            stock.cantidad += self.qty

        stock.actualizado_en_operacion = timezone.now()
        stock.save()
        
        # ✅ Guardamos el saldo en el movimiento
        self.saldo_resultante = stock.cantidad
        self.save(update_fields=['saldo_resultante'])


# ============================================================
# Usuarios / Roles
# ============================================================
class UserProfile(TimeStampedModel):
    class Rol(models.TextChoices):
        SOLICITANTE = "SOLICITANTE", "Solicitante (Técnico)"
        ALMACEN = "ALMACEN", "Almacén"
        ADMIN = "ADMIN", "Administrador"
        JEFA = "JEFA", "Jefa / Global"
        DISENADOR = "DISENADOR", "Diseñador / Planificador" 

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="profile")
    rol = models.CharField(max_length=15, choices=Rol.choices, default=Rol.SOLICITANTE)

    sede_principal = models.ForeignKey(
        Sede,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="usuarios_principales",
        help_text="Sede fija del usuario (técnico o almacén).",
    )
    sedes_permitidas = models.ManyToManyField(
        Sede,
        blank=True,
        related_name="usuarios_globales",
        help_text="Sedes a las que puede acceder (usuarios globales).",
    )
    sede_activa = models.ForeignKey(
        Sede,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="usuarios_activos",
        help_text="Sede seleccionada (usuarios globales).",
    )

    def __str__(self):
        return f"{self.user.username} ({self.rol})"

    def get_sede_operativa(self):
        if self.sede_principal:
            return self.sede_principal
        if self.sede_activa:
            return self.sede_activa
        return self.sedes_permitidas.first()


# ============================================================
# Serializados
# ============================================================
class ItemSerializado(TimeStampedModel):
    class Estado(models.TextChoices):
        EN_ALMACEN = "EN_ALMACEN", "En almacén"
        ASIGNADO = "ASIGNADO", "Asignado (salida)"
        INSTALADO = "INSTALADO", "Instalado"
        MERMA = "MERMA", "Merma / Baja"

    producto = models.ForeignKey(Producto, on_delete=models.PROTECT, related_name="items_serializados")
    serial = models.CharField(max_length=64, unique=True)
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.PROTECT, related_name="items_serializados")
    estado = models.CharField(max_length=15, choices=Estado.choices, default=Estado.EN_ALMACEN)
    asignado_a = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="items_asignados")

    class Meta:
        indexes = [
            models.Index(fields=["serial"]),
            models.Index(fields=["producto", "estado"]),
            models.Index(fields=["ubicacion"]),
        ]

    def clean(self):
        if self.serial:
            self.serial = self.serial.strip().upper()

    def __str__(self):
        return f"{self.producto.nombre} | {self.serial} | {self.estado}"


# ============================================================
# Proveedores
# ============================================================
class Proveedor(TimeStampedModel):
    ruc = models.CharField(max_length=11, unique=True)
    razon_social = models.CharField(max_length=255)
    direccion = models.CharField(max_length=255, blank=True, default="")
    telefono = models.CharField(max_length=50, blank=True, default="")
    email = models.EmailField(blank=True, default="")
    activo = models.BooleanField(default=True)

    class Meta:
        ordering = ["razon_social"]

    def clean(self):
        if self.ruc:
            r = self.ruc.strip()
            if not re.match(r"^\d{11}$", r):
                raise ValidationError({"ruc": "RUC debe tener 11 dígitos."})
            self.ruc = r

    def __str__(self):
        return f"{self.razon_social} ({self.ruc})"


# ============================================================
# Documentos: REQ/SAL/ING/MER
# ============================================================
class TipoDocumento(models.TextChoices):
    REQ = "REQ", "Requerimiento"
    SAL = "SAL", "Salida"
    ING = "ING", "Ingreso"
    MER = "MER", "Merma"


class EstadoDocumento(models.TextChoices):
    REQ_BORRADOR = "REQ_BORRADOR", "REQ - Borrador"
    REQ_PENDIENTE = "REQ_PENDIENTE", "REQ - Pendiente"
    REQ_ATENDIDO = "REQ_ATENDIDO", "REQ - Atendido"
    REQ_RECHAZADO = "REQ_RECHAZADO", "REQ - Rechazado"
    BORRADOR = "BORRADOR", "Borrador"
    CONFIRMADO = "CONFIRMADO", "Confirmado"
    ANULADO = "ANULADO", "Anulado"


class TipoRequerimiento(models.TextChoices):
    LOCAL = "LOCAL", "Local (técnico)"
    PROVEEDOR = "PROVEEDOR", "Proveedor"
    ENTRE_SEDES = "ENTRE_SEDES", "Entre sedes"


class Correlativo(models.Model):
    tipo = models.CharField(max_length=3, choices=TipoDocumento.choices, unique=True)
    ultimo_numero = models.PositiveBigIntegerField(default=0)

    def __str__(self):
        return f"{self.tipo} -> {self.ultimo_numero}"


class DocumentoInventario(models.Model):
    tipo = models.CharField(max_length=3, choices=TipoDocumento.choices)
    numero = models.CharField(max_length=32, unique=True, null=True, blank=True)
    fecha = models.DateTimeField(default=timezone.now)

    sede = models.ForeignKey(Sede, on_delete=models.PROTECT, related_name="documentos", null=True, blank=True)
    ubicacion = models.ForeignKey(Ubicacion, on_delete=models.PROTECT, related_name="documentos", null=True, blank=True)

    sede_origen = models.ForeignKey(
        Sede, on_delete=models.PROTECT, related_name="documentos_origen", null=True, blank=True
    )
    sede_destino = models.ForeignKey(
        Sede, on_delete=models.PROTECT, related_name="documentos_destino", null=True, blank=True
    )

    proveedor = models.ForeignKey(
        Proveedor, on_delete=models.PROTECT, null=True, blank=True, related_name="requerimientos"
    )

    centro_costo = models.CharField(max_length=255, blank=True, default="")
    
    responsable = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="documentos_inventario"
    )
    
    # ✅ CAMPO NUEVO: SOLICITANTE
    solicitante = models.ForeignKey(
        settings.AUTH_USER_MODEL, 
        on_delete=models.PROTECT, 
        related_name="documentos_solicitados", 
        null=True, 
        blank=True,
        help_text="Técnico o responsable de obra que solicita el material."
    )

    # ✅ CAMPO NUEVO: REFERENCIA
    referencia = models.CharField(
        max_length=100, 
        blank=True, 
        default="", 
        help_text="Ej: Código de Obra, Nro Factura, etc."
    )

    entregado_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="documentos_entregados", null=True, blank=True
    )

    estado = models.CharField(max_length=20, choices=EstadoDocumento.choices, default=EstadoDocumento.REQ_BORRADOR)
    observaciones = models.TextField(blank=True, default="")
    origen = models.ForeignKey("self", null=True, blank=True, on_delete=models.SET_NULL, related_name="derivados")

    recibido = models.BooleanField(default=False)
    recibido_por = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="documentos_recibidos", null=True, blank=True
    )
    recibido_en = models.DateTimeField(null=True, blank=True)

    tipo_requerimiento = models.CharField(
        max_length=20,
        choices=TipoRequerimiento.choices,
        default=TipoRequerimiento.LOCAL,
    )

    class Meta:
        ordering = ["-fecha"]
        indexes = [
            models.Index(fields=["tipo", "numero"]),
            models.Index(fields=["fecha"]),
            models.Index(fields=["tipo", "estado"]),
            models.Index(fields=["sede"]),
        ]

    def __str__(self):
        return f"{self.tipo} {self.numero or '(sin número)'}"

    def clean(self):
        if self.tipo == TipoDocumento.REQ and not self.sede_id:
            raise ValidationError({"sede": "REQ requiere sede (almacén solicitante)."})
        if self.tipo in (TipoDocumento.SAL, TipoDocumento.ING, TipoDocumento.MER) and not self.sede_id:
            raise ValidationError({"sede": "Este documento requiere sede (almacén)."})
        if self.ubicacion_id and self.sede_id and self.ubicacion.sede_id != self.sede_id:
            raise ValidationError({"ubicacion": "La ubicación no pertenece a la sede seleccionada."})

        if self.tipo == TipoDocumento.REQ:
            if self.tipo_requerimiento == TipoRequerimiento.LOCAL:
                if self.proveedor_id:
                    raise ValidationError({"proveedor": "REQ LOCAL no debe tener proveedor."})
                if self.sede_destino_id:
                    raise ValidationError({"sede_destino": "REQ LOCAL no usa sede_destino."})
            elif self.tipo_requerimiento == TipoRequerimiento.PROVEEDOR:
                if self.sede and self.sede.tipo != Sede.CENTRAL:
                    raise ValidationError({"sede": "Solo la sede CENTRAL puede generar REQ a PROVEEDOR."})
                if not self.proveedor_id:
                    raise ValidationError({"proveedor": "REQ a PROVEEDOR requiere proveedor."})
            elif self.tipo_requerimiento == TipoRequerimiento.ENTRE_SEDES:
                if self.sede and self.sede.tipo == Sede.CENTRAL:
                    raise ValidationError({"sede": "La sede CENTRAL no debe generar REQ 'ENTRE SEDES'."})
                if not self.sede_destino_id:
                    raise ValidationError({"sede_destino": "REQ entre sedes requiere sede_destino CENTRAL (Jauja)."})

    def _formatear_numero(self, correlativo: int) -> str:
        return f"{self.tipo}-{correlativo:010d}"

    @transaction.atomic
    def asignar_numero_si_falta(self):
        if self.numero:
            return self.numero
        corr, _ = Correlativo.objects.select_for_update().get_or_create(tipo=self.tipo)
        corr.ultimo_numero += 1
        corr.save(update_fields=["ultimo_numero"])
        self.numero = self._formatear_numero(corr.ultimo_numero)
        self.save(update_fields=["numero"])
        return self.numero

    @transaction.atomic
    def enviar_req(self):
        if self.tipo != TipoDocumento.REQ:
            raise ValidationError("Solo un REQ puede enviarse.")
        if self.estado != EstadoDocumento.REQ_BORRADOR:
            raise ValidationError("Solo puedes enviar un REQ en borrador.")
        if not self.items.exists():
            raise ValidationError("No puedes enviar un REQ sin ítems.")

        self.full_clean()
        self.asignar_numero_si_falta()
        self.estado = EstadoDocumento.REQ_PENDIENTE
        self.save(update_fields=["estado", "numero"])

    def _get_usuario_almacen_de_sede(self, sede: Sede):
        perfil = (
            UserProfile.objects.select_related("user", "sede_principal")
            .filter(rol=UserProfile.Rol.ALMACEN, sede_principal=sede)
            .order_by("creado_en")
            .first()
        )
        return perfil.user if perfil else None

    @transaction.atomic
    def confirmar(self, *, entregado_por=None):
        if self.tipo == TipoDocumento.REQ:
            raise ValidationError("Un REQ no se confirma; se envía y luego se atiende.")
        if self.estado != EstadoDocumento.BORRADOR:
            raise ValidationError("Solo se puede confirmar un documento en BORRADOR.")
        
        # ✅ USAMOS TU RELACIÓN 'items' (DocumentoItem)
        items = list(self.items.select_related("producto"))
        if not items:
            raise ValidationError("No puedes confirmar un documento sin ítems.")

        self.asignar_numero_si_falta()

        for it in items:
            if self.tipo == TipoDocumento.ING:
                mov_tipo = MovimientoInventario.TIPO_IN
                qty_mov = int(it.cantidad)
            elif self.tipo in (TipoDocumento.SAL, TipoDocumento.MER):
                mov_tipo = MovimientoInventario.TIPO_OUT
                qty_mov = int(it.cantidad)
            else:
                raise ValidationError("Tipo no soportado para confirmar.")

            # ✅ CREAMOS EL MOVIMIENTO CORRECTAMENTE CON TUS CAMPOS
            mov = MovimientoInventario.objects.create(
                producto=it.producto,
                sede=self.sede,
                ubicacion=self.ubicacion,
                tipo=mov_tipo,
                qty=qty_mov,
                # costo_unitario=it.costo_unitario,
                referencia=self.numero,
                usuario=self.responsable, # Registramos quién hizo el movimiento
                nota=it.observacion or "",
            )
            # ✅ APLICAMOS EL MOVIMIENTO (RESTAMOS STOCK)
            mov.aplicar()

        if entregado_por:
            self.entregado_por = entregado_por

        self.estado = EstadoDocumento.CONFIRMADO
        self.save(update_fields=["estado", "entregado_por"])

        # Lógica de transferencias (Sin cambios)
        if self.tipo == TipoDocumento.SAL and self.origen_id:
            req = self.origen
            if req and req.tipo == TipoDocumento.REQ and req.estado == EstadoDocumento.REQ_PENDIENTE:
                req.estado = EstadoDocumento.REQ_ATENDIDO
                req.save(update_fields=["estado"])

        if (self.tipo == TipoDocumento.SAL and self.sede_id and self.sede_destino_id and self.sede_destino_id != self.sede_id):
            if self.sede.tipo != Sede.CENTRAL:
                raise ValidationError("Solo la sede CENTRAL puede despachar transferencias.")

            ya_existe_ing = DocumentoInventario.objects.filter(tipo=TipoDocumento.ING, origen_id=self.id).exists()
            if not ya_existe_ing:
                responsable_destino = self._get_usuario_almacen_de_sede(self.sede_destino) or self.responsable

                ing = DocumentoInventario.objects.create(
                    tipo=TipoDocumento.ING,
                    sede=self.sede_destino,
                    sede_origen=self.sede,
                    sede_destino=self.sede_destino,
                    responsable=responsable_destino,
                    estado=EstadoDocumento.BORRADOR,
                    observaciones=f"ING generado por transferencia desde {self.numero}",
                    origen=self,
                )
                
                for it in self.items.all():
                    DocumentoItem.objects.create(
                        documento=ing,
                        producto=it.producto,
                        cantidad=it.cantidad,
                        costo_unitario=it.costo_unitario,
                        observacion=f"Recepción de {self.numero}",
                    )

        if self.tipo == TipoDocumento.ING and self.origen_id:
            sal = self.origen
            if sal and sal.tipo == TipoDocumento.SAL and sal.estado == EstadoDocumento.CONFIRMADO and not sal.recibido:
                sal.recibido = True
                sal.recibido_por = self.responsable
                sal.recibido_en = timezone.now()
                sal.save(update_fields=["recibido", "recibido_por", "recibido_en"])


class DocumentoItem(models.Model):
    documento = models.ForeignKey(DocumentoInventario, on_delete=models.CASCADE, related_name="items")
    producto = models.ForeignKey(Producto, on_delete=models.PROTECT)

    cantidad = models.PositiveIntegerField(default=1)
    costo_unitario = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    observacion = models.CharField(max_length=255, blank=True, default="")

    cantidad_devuelta = models.PositiveIntegerField(default=0)
    cantidad_merma = models.PositiveIntegerField(default=0)
    cantidad_usada = models.PositiveIntegerField(default=0)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["documento", "producto"], name="uq_doc_item_producto"),
        ]
        indexes = [
            models.Index(fields=["documento", "producto"]),
        ]

    def __str__(self):
        return f"{self.documento.tipo} {self.producto.codigo_interno} x {self.cantidad}"

    def clean(self):
        total = int(self.cantidad_devuelta or 0) + int(self.cantidad_merma or 0) + int(self.cantidad_usada or 0)
        if total > int(self.cantidad or 0):
            raise ValidationError("Devuelto + Merma + Usado no puede superar la cantidad entregada.")

    def save(self, *args, **kwargs):
        if self.costo_unitario is None:
            self.costo_unitario = self.producto.costo_unitario
        super().save(*args, **kwargs)