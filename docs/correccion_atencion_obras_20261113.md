# Obras pendientes de atención en Almacén

## Comportamiento corregido

La bandeja “Proyectos por Atender” incluía todas las obras aprobadas o en proceso, aunque ya se hubieran entregado todos sus materiales. El contador del dashboard, además, utilizaba otros estados y sedes.

Listado y contador ahora comparten la misma consulta: obras aprobadas o en proceso con algún producto cuyo planificado supere lo entregado, o algún material pendiente de catálogo sin resolver. Cada producto se compara por separado: un exceso de entrega en uno no compensa el faltante de otro.

Una obra completamente abastecida deja de aparecer en esa bandeja y sigue disponible para consulta y liquidación. La consulta no finaliza obras ni cambia estados, materiales, stock, movimientos o asignaciones.

El detalle muestra por separado los materiales pendientes de catálogo, aclara a qué corresponde el porcentaje de entrega y muestra correctamente las cantidades pendientes. Oculta “Generar Salida” cuando no hay material catalogado pendiente de entrega. Un acceso directo explica si falta vincular productos o si no hay materiales planificados.

## Validación

Las regresiones cubren entrega completa, entrega parcial, exceso de un producto con faltante de otro, pendientes de catálogo, resolución del catálogo, obras vacías, atención multisede, duplicados, concordancia del contador y permisos de Almacén. Una obra completa conserva su estado y permanece en liquidaciones.

No requiere migraciones ni cambios en registros existentes. No modifica responsables de obras ni sus documentos históricos.
