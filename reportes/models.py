# -*- coding: utf-8 -*-
"""
Recepción de los materiales declarados en campo (SICV).

Este módulo hace UNA cosa: recibir y conservar lo que manda SICV, tal como
lo manda. No cruza nada contra nuestro catálogo -eso viene después- y esa
separación es deliberada: si la recepción dependiera del cruce, una fila
cuyo material no reconocemos no se podría guardar, y entonces el material
declarado en campo nunca cuadraría contra el almacén.

De ahí la regla §8.1 del contrato: **nunca descartar una fila**. Entra
completa aunque no entendamos ninguno de sus nombres.
"""
import hashlib

from django.conf import settings
from django.db import models

from inventario.models import Sede, TimeStampedModel


class SincronizacionSicv(TimeStampedModel):
    """
    Una corrida de recepción.

    Existe para poder responder "¿de dónde salió esta fila y cuándo entró?"
    y para que una corrida a medias se distinga de una que no trajo nada.
    Esa diferencia importa: §8.3 pide que "integración apagada", "sin
    movimientos" y "SICV no respondió" se vean distinto en pantalla.
    """

    class Estado(models.TextChoices):
        EN_CURSO = "EN_CURSO", "En curso"
        COMPLETADA = "COMPLETADA", "Completada"
        FALLIDA = "FALLIDA", "Fallida"

    class Origen(models.TextChoices):
        API = "API", "API de SICV"
        ARCHIVO = "ARCHIVO", "Archivo importado"

    sede = models.ForeignKey(
        Sede,
        on_delete=models.PROTECT,
        related_name="sincronizaciones_sicv",
        help_text="SICV exporta por sede; el reporte no la trae en cada fila.",
    )
    desde = models.DateField()
    hasta = models.DateField()

    origen = models.CharField(max_length=10, choices=Origen.choices, default=Origen.API)
    estado = models.CharField(max_length=12, choices=Estado.choices, default=Estado.EN_CURSO)

    filas_recibidas = models.PositiveIntegerField(default=0)
    filas_nuevas = models.PositiveIntegerField(default=0)
    filas_repetidas = models.PositiveIntegerField(default=0)

    detalle_error = models.TextField(blank=True, default="")

    iniciada_en = models.DateTimeField(auto_now_add=True)
    terminada_en = models.DateTimeField(null=True, blank=True)
    ejecutada_por = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="sincronizaciones_sicv",
    )

    class Meta:
        verbose_name = "Sincronización SICV"
        verbose_name_plural = "Sincronizaciones SICV"
        ordering = ["-iniciada_en"]
        indexes = [
            models.Index(fields=["sede", "-iniciada_en"]),
            models.Index(fields=["estado"]),
        ]

    def __str__(self):
        return f"SICV {self.sede.nombre} {self.desde:%d/%m/%Y}-{self.hasta:%d/%m/%Y} ({self.estado})"


class DetalleMaterialSicv(TimeStampedModel):
    """
    Una fila del reporte "Registro de materiales" de SICV.

    Los campos guardan el texto recibido **sin normalizar**. No es
    dejadez: es lo que permite volver a interpretarlo cuando se definan las
    equivalencias contra nuestro catálogo, sin tener que pedirle los datos a
    SICV de nuevo.
    """

    sincronizacion = models.ForeignKey(
        SincronizacionSicv,
        on_delete=models.CASCADE,
        related_name="detalles",
    )
    sede = models.ForeignKey(
        Sede,
        on_delete=models.PROTECT,
        related_name="detalles_sicv",
    )

    id_origen = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Identificador de la fila en SICV, si lo provee.",
    )
    orden = models.CharField(max_length=32, db_index=True)
    tipo_orden = models.CharField(max_length=80, blank=True, default="")
    emision = models.DateTimeField(null=True, blank=True)

    codigo_abonado = models.CharField(max_length=40, blank=True, default="")
    abonado = models.CharField(max_length=200, blank=True, default="")
    direccion = models.CharField(max_length=255, blank=True, default="")

    material = models.CharField(
        max_length=200,
        help_text="Nombre del material como lo escribe SICV.",
    )
    # Decimal y no entero: SICV declara 19.00 de cable, y truncar metros a
    # unidades enteras haría que el consumo de campo nunca cuadre.
    cantidad = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    accion = models.CharField(
        max_length=40,
        blank=True,
        default="",
        help_text="Qué se hizo con el material: Utilizado, Retirado, etc.",
    )
    mac = models.CharField(max_length=40, blank=True, default="")

    atencion = models.DateTimeField(null=True, blank=True)
    situacion = models.CharField(max_length=40, blank=True, default="", db_index=True)
    tecnico = models.CharField(max_length=200, blank=True, default="")

    # Identidad de la fila, para que reimportar el mismo periodo actualice
    # en vez de duplicar. Se calcula en save() y no se toca a mano.
    huella = models.CharField(max_length=64, unique=True, editable=False)

    class Meta:
        verbose_name = "Material declarado en SICV"
        verbose_name_plural = "Materiales declarados en SICV"
        ordering = ["-atencion", "orden"]
        indexes = [
            models.Index(fields=["sede", "atencion"]),
            models.Index(fields=["material"]),
            models.Index(fields=["tecnico"]),
        ]

    def __str__(self):
        return f"Orden {self.orden} · {self.material} x{self.cantidad}"

    def calcular_huella(self):
        """
        Clave estable de la fila.

        Si SICV manda un identificador propio se usa ese, que es lo
        correcto. Si no, se arma con los campos que juntos identifican una
        línea del reporte: una orden usa varios materiales, así que el
        número de orden solo no alcanza -la segunda línea pisaría a la
        primera y el consumo declarado quedaría corto sin que nada avise-.
        """
        if self.id_origen:
            semilla = f"sicv:{self.sede_id}:{self.id_origen}"
        else:
            semilla = "|".join([
                f"sicv:{self.sede_id}",
                self.orden or "",
                self.material or "",
                self.mac or "",
                self.accion or "",
                f"{self.cantidad}",
                self.atencion.isoformat() if self.atencion else "",
            ])
        return hashlib.sha256(semilla.encode("utf-8")).hexdigest()

    def save(self, *args, **kwargs):
        self.huella = self.calcular_huella()
        super().save(*args, **kwargs)
