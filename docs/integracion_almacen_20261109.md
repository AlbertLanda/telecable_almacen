# Integración de Almacén — revisión del 9 de noviembre de 2026

## Base de integración

- `main`: `a556fd116149ebb64acf9a75cc8eeb1e21f25c5a`.
- Kardex (`dev/fabrizio`): `15215614a181540774e099d09a7bfc3efb6aa268`.
- Firmas (`feat/firma-documentos`): `257559fab0744a8933cfa478dccedd7bcc6843aa`.
- Obras multisede (`fix/liquidacion-obras-multisede`): `5d58b188930b69c4358659ac403785fc6084767c`.

La paginación compacta de `main` se conserva junto con los filtros de firmas.
El despliegue de Azure continúa dependiendo de un cambio en `main`.

## Correcciones

- El Kardex calcula saldos documentales con una única lectura SQL de stock y movimientos. Solo los movimientos ADJ cuentan como ajustes. La diferencia con stock actual aparece separada como pendiente de conciliación, sin generar movimientos ni cambiar inventarios.
- Las firmas cubren seriales, trazabilidad y estado histórico consignado, además del contenido impreso. Los movimientos posteriores de custodia no invalidan un documento anterior. Una firma anterior cuyo contenido no se pueda verificar queda como contenido cambiado y conserva su evidencia original.
- Las impresiones mantienen su acceso operativo, pero ocultan trazos y datos del firmante a usuarios ajenos. Muestran “Firmas restringidas” en ese caso. Almacén conserva la gestión multisede de obras.
- Los despachos y cierres bloquean y vuelven a validar el mismo proyecto dentro de la transacción. La fecha estimada no se usa como prueba de liquidación. En reaperturas, las cantidades usadas antes dejan de estar pendientes.
- El cierre de productos serializados exige que las cantidades pendientes coincidan con los equipos vinculados y bloquea esas unidades durante la operación. Una inconsistencia revierte la transacción completa.
- Las obras con acta firmada no pueden reabrirse desde administración ni recibir un nuevo despacho/cierre. Para ampliar una obra ya firmada se debe crear otra obra; se conserva el acta anterior.
- No se descuenta mochila personal automáticamente a partir de una fecha de corte. Los antecedentes ambiguos requieren conciliación explícita.
- Los comandos de demostración exigen `DEBUG=True` y habilitación expresa con `ALLOW_DEMO_DATA=1`; rechazan la ejecución sin consultar datos cuando no se cumplen ambas condiciones.

## Migraciones y preservación

Nuevas migraciones: `inventario.0022`, `inventario.0023`, `proyectos.0010` y `proyectos.0011`.
Crean el registro de firmas y campos de liquidación. El backfill de `0011` solo marca evidencia de cierre: no modifica stock, custodias, documentos ni movimientos.

Se probaron sobre una copia local: los valores de las columnas originales de 33 tablas quedaron idénticos; la base de origen conservó su SHA-256. Esta comprobación corresponde a la copia local, no a una copia de producción actualizada.

## Obras antiguas con mochila ambigua

La consulta siguiente es de solo lectura:

```sh
python manage.py revisar_mochila_obra --codigo CODIGO-DE-OBRA
```

TI debe revisar los despachos, devoluciones y existencias personales y conciliar el caso por separado. Solo después se registra esa revisión:

```sh
python manage.py revisar_mochila_obra --codigo CODIGO-DE-OBRA --confirmar --usuario SUPERUSUARIO --motivo "Referencia de la conciliación realizada"
```

El comando exige un superusuario activo y motivo. Registra una anotación de auditoría y el indicador de revisión; **no corrige cantidades** ni sirve como sustituto de la conciliación. Ejecutar con la configuración y el entorno de la base que realmente se desea revisar.

## Validación manual antes de aprobar

1. Revisar la paginación del historial y los filtros de firmas.
2. Abrir el Kardex de un producto en varios meses, verificando que consultar el informe no cambie existencias y que las diferencias de stock se muestren separadas.
3. En la copia de pruebas, firmar una salida, comprobar las dos firmas y validar que un técnico ajeno no pueda ver los trazos.
4. Despachar material de obra desde una sede y liquidarlo en otra; verificar almacén receptor, cantidades pendientes y mochila personal.
5. Repetir el envío de cierre y comprobar que no se genera un segundo ingreso.
6. Probar una obra antigua sin evidencia suficiente: debe pedir conciliación y conservar las cantidades.

Las comprobaciones automatizadas de concurrencia requieren PostgreSQL y se ejecutan en el workflow de pruebas del PR. La aprobación del PR y el despliegue se realizan después de revisar esos resultados y completar la validación funcional.
