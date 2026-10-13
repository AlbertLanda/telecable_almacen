# Custodia de equipos por sede

La mochila numérica se separaba por técnico, producto y sede, pero la liquidación
consultaba los equipos por técnico y producto. Liquidar una sede podía devolver
o instalar seriales entregados por otra. Al despachar se borraba la ubicación sin
conservar la sede responsable de la mochila.

`ItemSerializado.sede_asignacion` conserva esa sede. Las salidas guardan los
seriales concretos; las liquidaciones, devoluciones y recepciones validan sede,
producto, responsable, estado y cantidad dentro de una transacción. Las obras
quedan fuera de la mochila personal. Un saldo que no coincide con sus seriales
bloquea la liquidación y muestra que requiere conciliación.

En el módulo de liquidación semanal por sede, ALMACEN solo puede operar y
consultar liquidaciones de su sede principal. Ser almacén CENTRAL o tener otras
sedes en `sedes_permitidas` no concede acceso global. El consolidado central
queda reservado a ADMIN y JEFA. Una cuenta ALMACEN sin sede principal se bloquea.
Esto no cambia perfiles existentes ni constituye una revisión integral de los
permisos de otros módulos. Las plantillas y funciones SQL del módulo antiguo de
liquidación por sede no están incluidas completamente en este repositorio;
las pruebas de permisos comprueban que las llamadas no autorizadas se bloquean
antes de invocar ese servicio, sin afirmar que validen su cálculo SQL.

## Despliegue y registros anteriores

1. Revisar el PR y las pruebas con PostgreSQL 15. El despliegue requiere las
   migraciones `inventario.0020` y `0021` junto con el código.
2. Coordinar una ventana sin despachos, préstamos ni liquidaciones y conservar
   una copia recuperable de PostgreSQL antes de migrar o conciliar.
3. Desplegar la revisión aprobada por el flujo habitual de Azure. Verificar las
   migraciones y `python manage.py check --settings=config.settings.prod`.
4. Conciliar custodias anteriores con comprobantes de entrega. La migración
   deja su sede en NULL: **no toma la sede del perfil ni adivina por cantidad**.
   Sus liquidaciones quedan bloqueadas hasta conciliarlas. Revisar también los
   préstamos, devoluciones y transferencias en tránsito anteriores al cambio.
5. Verificar una salida, devolución y liquidación controladas en cada sede
   antes de reabrir las operaciones habituales.

Para equipos antiguos ASIGNADOS al técnico correcto, usar primero la simulación
de `conciliar_sede_seriales`. Este ejemplo usa identificadores ficticios:

```bash
python manage.py conciliar_sede_seriales --settings=config.settings.prod \
  --salida SAL-EJEMPLO --producto PROD-EJEMPLO --codigos 801,802,803
```

Solo tras revisar el comprobante se puede añadir `--aplicar --usuario USUARIO_ADMIN
--motivo "Evidencia de entrega revisada"`. No cambia cantidades, estados ni
responsables. Rechaza equipos vinculados a otra sede. No sirve para resolver
transferencias en tránsito o equipos con estado incorrecto.

## Reparación mediante un plan privado

`reparar_custodia_entre_sedes` resuelve un caso concreto de dos equipos que siguen
físicamente con un técnico: uno figura instalado y otro devuelto a otra sede.
No es un reconciliador general. Exige que la mochila de origen ya sea de dos
unidades y que los demás datos coincidan exactamente con el diagnóstico.

Guardar fuera del repositorio un JSON privado con esta estructura. Todos los
valores del ejemplo son ficticios y deben sustituirse tras revisar el caso:

```json
{
  "clave": "reparacion-ejemplo-v1",
  "tecnico": "tecnico_ejemplo",
  "producto": "PROD-EJEMPLO",
  "sede_origen": "SEDE-A",
  "sede_destino": "SEDE-B",
  "salida": "SAL-EJEMPLO",
  "ingreso": "ING-EJEMPLO",
  "mochila_destino": 3,
  "retorno": 4,
  "consumo": 2,
  "merma": 0,
  "instalado": {"codigo": "701", "serial": "SN-EJEMPLO-A"},
  "en_almacen": {"codigo": "702", "serial": "SN-EJEMPLO-B"}
}
```

Simular después de desplegar las migraciones:

```bash
python manage.py reparar_custodia_entre_sedes --settings=config.settings.prod \
  --plan /home/plan_privado.json
```

La reparación restituye ambos equipos a ASIGNADO, al técnico y sede de origen,
vinculándolos a su salida anterior. No crea otra salida ni incrementa mochilas.
Revierte una unidad ingresada erróneamente al almacén destino mediante ADJ -1;
conserva el almacén de origen y los otros equipos. Mantiene las cantidades del
ingreso histórico y agrega una observación con el ajuste, sin inventar consumo.

**La ubicación actual no demuestra qué ingreso incluyó el equipo.** Antes de
aplicar, el responsable debe confirmar el retorno erróneo documentado. El comando
exige `--aplicar`, `--usuario`, `--confirmar-retorno NUMERO_INGRESO` y
`--stock-destino-esperado SALDO_REVISADO`. Si no se confirma el retorno, o cambió
el diagnóstico, se necesita otro plan. No modificar el JSON para forzar su paso.

La reparación y su respaldo en `CorreccionSeriales` comparten una transacción.
Repetir el mismo plan no vuelve a ajustar stock. El respaldo permite preparar
una reversión revisada; no sustituye una copia completa de PostgreSQL.

Verificar después los dos seriales y saldo en la sede de origen, los demás saldos
sin cambios y un único ajuste del caso en el kardex. Los totales impresos del
ingreso histórico se conservan: su observación y el ajuste explican la corrección.

## Pruebas

```bash
python manage.py test --settings=config.settings.test
python manage.py makemigrations --check --dry-run --settings=config.settings.test
```

Local usa SQLite aislado. CI usa PostgreSQL 15 y Python 3.11, incluyendo dos
liquidaciones simultáneas. Las pruebas incluyen seriales de otra sede, selección
duplicada, equipos de obra, doble envío, préstamos, transferencias y reparación
atómica e idempotente, con datos ficticios.
