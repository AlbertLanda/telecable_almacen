# -*- coding: utf-8 -*-
"""
Radiografía de solo lectura para dimensionar el módulo de reportes.

No escribe nada ni imprime datos personales: solo conteos, porcentajes y
nombres de maestros (sedes, categorías). La salida está pensada para
pegarse en un chat sin exponer seriales, MACs, abonados ni usuarios.
"""
from django.core.management.base import BaseCommand
from django.db.models import Count

from inventario.models import (
    Categoria,
    DocumentoInventario,
    DocumentoItemSerializado,
    ItemSerializado,
    MovimientoInventario,
    Producto,
    Proveedor,
    Sede,
    Stock,
    StockTecnico,
    UserProfile,
)


def pct(parte, total):
    return f"{(parte * 100 / total):.0f}%" if total else "—"


class Command(BaseCommand):
    help = "Conteos del módulo de reportes. No modifica nada."

    def linea(self, etiqueta, valor, nota=""):
        self.stdout.write(f"  {etiqueta:<42} {valor}{('  ' + nota) if nota else ''}")

    def titulo(self, texto):
        self.stdout.write("")
        self.stdout.write(self.style.MIGRATE_HEADING(texto))

    def handle(self, *args, **opciones):
        # ---------------- Sedes y usuarios ----------------
        self.titulo("SEDES")
        for sede in Sede.objects.order_by("nombre"):
            usuarios = UserProfile.objects.filter(sede_principal=sede).values("rol").annotate(n=Count("id"))
            detalle = ", ".join(f"{r['rol']}:{r['n']}" for r in usuarios) or "sin usuarios"
            self.linea(f"{sede.nombre} ({sede.tipo})", detalle)

        # ---------------- Catálogo ----------------
        productos = Producto.objects.filter(activo=True)
        total_prod = productos.count()

        self.titulo("CATÁLOGO")
        self.linea("Productos activos", total_prod)
        self.linea("Productos inactivos", Producto.objects.filter(activo=False).count())
        self.linea("Categorías", Categoria.objects.count())
        self.linea("Sin categoría", productos.filter(categoria__isnull=True).count())

        # Si casi nadie tiene mínimo o costo, dos columnas del reporte salen
        # siempre vacías y conviene saberlo antes de que alguien las mire.
        con_minimo = productos.filter(stock_minimo__gt=0).count()
        con_costo = productos.filter(costo_unitario__gt=0).count()
        self.linea("Con stock mínimo definido", con_minimo, f"({pct(con_minimo, total_prod)})")
        self.linea("Con costo unitario > 0", con_costo, f"({pct(con_costo, total_prod)})")
        self.linea("Serializados", productos.filter(es_serializado=True).count())
        self.linea("Marcados como activo/herramienta", productos.filter(es_activo=True).count())
        self.linea("Con código de barras", productos.exclude(barcode__isnull=True).exclude(barcode="").count())

        # ---------------- Existencias ----------------
        self.titulo("EXISTENCIAS")
        self.linea("Filas de Stock (producto × sede)", Stock.objects.count())
        self.linea("Con cantidad > 0", Stock.objects.filter(cantidad__gt=0).count())
        self.linea("Con cantidad negativa", Stock.objects.filter(cantidad__lt=0).count(),
                   "<-- revisar si no es 0")
        self.linea("Mochilas de técnico (StockTecnico)", StockTecnico.objects.count())

        # ---------------- Movimientos ----------------
        movimientos = MovimientoInventario.objects.count()
        self.titulo("MOVIMIENTOS")
        self.linea("Total", movimientos, "<-- define si el export aguanta")
        for tipo, etiqueta in MovimientoInventario.TIPOS:
            self.linea(f"  {etiqueta}", MovimientoInventario.objects.filter(tipo=tipo).count())

        primero = MovimientoInventario.objects.order_by("creado_en").values_list("creado_en", flat=True).first()
        ultimo = MovimientoInventario.objects.order_by("-creado_en").values_list("creado_en", flat=True).first()
        if primero:
            self.linea("Rango de fechas", f"{primero:%d/%m/%Y} a {ultimo:%d/%m/%Y}")

        # Las columnas Obra, Técnico y Proveedor del export salen de unir
        # movimiento.referencia con documento.numero. Este porcentaje dice
        # cuántas filas van a traerlas llenas y cuántas en blanco.
        numeros = set(
            DocumentoInventario.objects.exclude(numero__isnull=True)
            .exclude(numero="").values_list("numero", flat=True)
        )
        enlazados = MovimientoInventario.objects.filter(referencia__in=numeros).count()
        self.linea("Con referencia que enlaza a un documento", enlazados,
                   f"({pct(enlazados, movimientos)}) <-- CLAVE")

        # ---------------- Documentos ----------------
        self.titulo("DOCUMENTOS")
        for fila in DocumentoInventario.objects.values("tipo").annotate(n=Count("id")).order_by("tipo"):
            self.linea(f"  {fila['tipo']}", fila["n"])
        self.linea("Con solicitante", DocumentoInventario.objects.filter(solicitante__isnull=False).count())
        self.linea("Con proveedor", DocumentoInventario.objects.filter(proveedor__isnull=False).count())
        self.linea("Proveedores registrados", Proveedor.objects.count())

        # ---------------- Equipos ----------------
        equipos = ItemSerializado.objects.count()
        self.titulo("EQUIPOS SERIALIZADOS")
        self.linea("Total", equipos)
        for estado, etiqueta in ItemSerializado.Estado.choices:
            self.linea(f"  {etiqueta}", ItemSerializado.objects.filter(estado=estado).count())

        con_mac = ItemSerializado.objects.exclude(mac_address__isnull=True).exclude(mac_address="").count()
        self.linea("Con MAC", con_mac, f"({pct(con_mac, equipos)})")

        # Un equipo despachado pierde la ubicación. Si este número es alto,
        # la pantalla de equipos (que filtra por ubicacion__sede) los está
        # escondiendo a todos.
        huerfanos = ItemSerializado.objects.filter(ubicacion__isnull=True).count()
        sin_rastro = ItemSerializado.objects.filter(
            ubicacion__isnull=True, asignado_a__isnull=True, proyecto__isnull=True
        ).count()
        self.linea("Sin ubicación", huerfanos, f"({pct(huerfanos, equipos)})")
        self.linea("Sin ubicación NI técnico NI obra", sin_rastro,
                   "<-- no se les puede asignar sede")

        # El enlace documento <-> serial. Si está en cero, no hay forma de
        # saber qué equipo salió en qué despacho.
        self.linea("Enlaces documento-serial", DocumentoItemSerializado.objects.count(),
                   "<-- CLAVE")

        self.stdout.write("")
        self.stdout.write(self.style.SUCCESS("Listo. No se modificó ningún dato."))
