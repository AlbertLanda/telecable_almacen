from django.conf import settings
from django.core.mail import send_mail
from django.db.models import F, Q

from inventario.models import Stock


def obtener_stocks_criticos(sede):
    return (
        Stock.objects
        .filter(sede=sede, producto__activo=True)
        .filter(
            Q(producto__stock_minimo__gt=0, cantidad__lte=F("producto__stock_minimo"))
            | Q(producto__stock_minimo=0, cantidad__lte=5)
        )
        .select_related("producto", "sede")
        .order_by("cantidad", "producto__nombre")
    )


def construir_mensaje_alerta_stock(sede):
    stocks = list(obtener_stocks_criticos(sede))

    if not stocks:
        return None, None

    asunto = f"🚨 Stock crítico en {sede.nombre}"

    lineas = [
        f"Se detectaron {len(stocks)} producto(s) con stock crítico en {sede.nombre}.",
        "",
        "Detalle:",
    ]

    for stock in stocks:
        producto = stock.producto
        unidad = getattr(producto, "unidad", "") or "UND"
        minimo = producto.stock_minimo or 0

        lineas.append(
            f"- {producto.nombre} ({producto.codigo_interno or 'SIN CÓDIGO'}): "
            f"stock actual {stock.cantidad} {unidad}, mínimo {minimo}."
        )

    lineas.extend([
        "",
        "Por favor revisar el panel de almacén.",
        "Sistema Telecable Almacén",
    ])

    return asunto, "\n".join(lineas)


def enviar_alerta_stock_por_correo(sede, destinatarios):
    asunto, mensaje = construir_mensaje_alerta_stock(sede)

    if not asunto:
        return False

    send_mail(
        subject=asunto,
        message=mensaje,
        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
        recipient_list=destinatarios,
        fail_silently=False,
    )

    return True