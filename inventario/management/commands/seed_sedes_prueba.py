from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from inventario.management.demo import require_demo_data
from inventario.models import (
    Categoria,
    MovimientoInventario,
    Producto,
    Sede,
    Ubicacion,
    UserProfile,
)
from proyectos.models import EstadoProyecto, Proyecto, ProyectoMaterial

PASSWORD = "test1234"

# Materiales de red (no serializados) con el stock inicial de cada sede.
PRODUCTOS = [
    # codigo,     nombre,                                   unidad, stock Jauja, stock Oroya
    ("PRB-001", "Cable drop FTTH 1 hilo",                    "MTS",  2000, 1200),
    ("PRB-002", "Conector rápido SC/APC",                    "UND",   300,  150),
    ("PRB-003", "Roseta óptica 2 puertos",                   "UND",   120,   60),
    ("PRB-004", "Grapa de anclaje para drop",                "UND",   500,  250),
    ("PRB-005", "Cinta aislante 3M",                         "UND",    80,   40),
    ("PRB-006", "Caja NAP 16 puertos",                       "UND",    10,    6),
]

# Dos obras aprobadas, con todo el material todavía por despachar.
OBRAS = [
    {
        "codigo": "OBRA-JAUJA-001",
        "nombre": "Ampliación red FTTH Jr. Junín - Jauja",
        "sede": "Sede Jauja",
        "responsable": "tecnico_resp_jauja",
        "materiales": {"PRB-001": 600, "PRB-002": 40, "PRB-003": 20, "PRB-004": 80, "PRB-006": 2},
    },
    {
        "codigo": "OBRA-OROYA-001",
        "nombre": "Tendido de fibra Av. Horacio Zevallos - La Oroya",
        "sede": "Sede Oroya",
        "responsable": "tecnico_resp_oroya",
        "materiales": {"PRB-001": 400, "PRB-002": 30, "PRB-004": 60, "PRB-005": 10, "PRB-006": 1},
    },
]


class Command(BaseCommand):
    help = (
        "Crea una base de pruebas con dos sedes (Sede Jauja y Sede Oroya), "
        "dos cuentas de almacén y dos técnicos (Resp y Crew) por sede, stock "
        "inicial de materiales y dos obras aprobadas con material por despachar. "
        "Se puede correr más de una vez: no duplica nada. "
        f"Password de todos los usuarios: {PASSWORD}"
    )

    def _usuario(self, username, nombre, apellido, rol, sede):
        user, creado = User.objects.get_or_create(
            username=username,
            defaults={"first_name": nombre, "last_name": apellido},
        )
        if creado:
            user.set_password(PASSWORD)
            user.save()
        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.rol = rol
        profile.sede_principal = sede
        profile.save()
        if sede:
            profile.sedes_permitidas.add(sede)
        return user

    def handle(self, *args, **options):
        require_demo_data()
        return self._cargar_demo(*args, **options)

    @transaction.atomic
    def _cargar_demo(self, *args, **options):
        # ---- Sedes (Jauja es la CENTRAL) y una ubicación en cada una ----
        jauja, _ = Sede.objects.get_or_create(
            nombre="Sede Jauja", defaults={"tipo": Sede.CENTRAL, "activo": True},
        )
        oroya, _ = Sede.objects.get_or_create(
            nombre="Sede Oroya", defaults={"tipo": Sede.SECUNDARIO, "activo": True},
        )
        sedes = {"Sede Jauja": jauja, "Sede Oroya": oroya}
        for sede in sedes.values():
            Ubicacion.objects.get_or_create(sede=sede, nombre="Almacén principal")

        # ---- Usuarios: 2 de almacén y 2 técnicos por sede ----
        A, T = UserProfile.Rol.ALMACEN, UserProfile.Rol.SOLICITANTE
        usuarios = [
            ("almacen_jauja_1", "Almacén", "Jauja Uno", A, jauja),
            ("almacen_jauja_2", "Almacén", "Jauja Dos", A, jauja),
            ("almacen_oroya_1", "Almacén", "Oroya Uno", A, oroya),
            ("almacen_oroya_2", "Almacén", "Oroya Dos", A, oroya),
            ("tecnico_resp_jauja", "Técnico Resp", "Jauja", T, jauja),
            ("tecnico_crew_jauja", "Técnico Crew", "Jauja", T, jauja),
            ("tecnico_resp_oroya", "Técnico Resp", "Oroya", T, oroya),
            ("tecnico_crew_oroya", "Técnico Crew", "Oroya", T, oroya),
            ("disenador_prueba", "Diseñador", "Prueba", UserProfile.Rol.DISENADOR, jauja),
        ]
        u = {nombre: self._usuario(nombre, n, ap, rol, sede) for nombre, n, ap, rol, sede in usuarios}

        # ---- Productos y stock inicial (con movimiento de entrada, para el kardex) ----
        categoria, _ = Categoria.objects.get_or_create(nombre="Materiales de red")
        productos = {}
        for codigo, nombre, unidad, stock_jauja, stock_oroya in PRODUCTOS:
            producto, _ = Producto.objects.get_or_create(
                codigo_interno=codigo,
                defaults={"nombre": nombre, "unidad": unidad, "categoria": categoria, "stock_minimo": 5},
            )
            productos[codigo] = producto
            for sede, cantidad, almacenero in (
                (jauja, stock_jauja, u["almacen_jauja_1"]),
                (oroya, stock_oroya, u["almacen_oroya_1"]),
            ):
                referencia = f"CARGA-INICIAL-{sede.nombre.split()[-1].upper()}"
                ya_cargado = MovimientoInventario.objects.filter(
                    producto=producto, sede=sede, referencia=referencia,
                ).exists()
                if not ya_cargado:
                    MovimientoInventario.objects.create(
                        producto=producto, sede=sede, tipo=MovimientoInventario.TIPO_IN,
                        qty=cantidad, referencia=referencia, usuario=almacenero,
                        nota="Stock inicial de la base de pruebas",
                    ).aplicar()

        # ---- Obras aprobadas con material por despachar ----
        for obra in OBRAS:
            proyecto, _ = Proyecto.objects.get_or_create(
                codigo=obra["codigo"],
                defaults={
                    "nombre": obra["nombre"],
                    "sede": sedes[obra["sede"]],
                    "creado_por": u["disenador_prueba"],
                    "responsable": u[obra["responsable"]],
                    "estado": EstadoProyecto.APROBADO,
                    "fecha_aprobacion": timezone.now(),
                },
            )
            for codigo, cantidad in obra["materiales"].items():
                ProyectoMaterial.objects.get_or_create(
                    proyecto=proyecto,
                    producto=productos[codigo],
                    defaults={"cantidad_planificada": cantidad, "cantidad_entregada": 0},
                )

        # ---- Resumen ----
        ok = self.style.SUCCESS
        self.stdout.write(ok(f"Base de pruebas lista. Password de todos: {PASSWORD}"))
        for sede in (jauja, oroya):
            self.stdout.write(f"\n{sede.nombre} ({sede.tipo})")
            for nombre, _, _, rol, s in usuarios:
                if s == sede and rol != UserProfile.Rol.DISENADOR:
                    self.stdout.write(f"  - {nombre:<20} {UserProfile.Rol(rol).label}")
        self.stdout.write(f"\nDiseñador (crea obras): disenador_prueba")
        self.stdout.write("\nObras aprobadas con material por despachar:")
        for obra in OBRAS:
            mats = ", ".join(f"{productos[c].nombre} x{q}" for c, q in obra["materiales"].items())
            self.stdout.write(f"  - {obra['codigo']} ({obra['sede']}, resp. {obra['responsable']}): {mats}")
