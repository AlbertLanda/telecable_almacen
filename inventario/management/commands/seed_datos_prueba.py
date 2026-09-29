from django.contrib.auth.models import User
from django.core.management.base import BaseCommand

from inventario.models import (
    Sede,
    Producto,
    UserProfile,
    MovimientoInventario,
)
from proyectos.models import Proyecto, ProyectoMaterial, EstadoProyecto


class Command(BaseCommand):
    help = (
        "Crea datos mínimos de prueba en una base vacía para poder probar "
        "el Informe de Movimientos (Kardex) y la visibilidad de material de "
        "cuadrilla para el técnico. Es seguro correrlo más de una vez "
        "(usa get_or_create). Password de todos los usuarios: test1234"
    )

    def _crear_usuario(self, username, rol, sede):
        user, created = User.objects.get_or_create(
            username=username,
            defaults={"first_name": username.replace("_", " ").title()},
        )
        if created:
            user.set_password("test1234")
            user.save()

        profile, _ = UserProfile.objects.get_or_create(user=user)
        profile.rol = rol
        profile.sede_principal = sede
        profile.save()

        return user

    def handle(self, *args, **options):
        sede, _ = Sede.objects.get_or_create(
            nombre="Sede Prueba",
            defaults={"tipo": Sede.SECUNDARIO, "activo": True},
        )

        producto, _ = Producto.objects.get_or_create(
            nombre="Clavos de Prueba",
            defaults={"unidad": "UND", "stock_minimo": 5},
        )

        almacen = self._crear_usuario("almacen_test", UserProfile.Rol.ALMACEN, sede)
        tecnico_resp = self._crear_usuario("tecnico_resp_test", UserProfile.Rol.SOLICITANTE, sede)
        tecnico_crew = self._crear_usuario("tecnico_crew_test", UserProfile.Rol.SOLICITANTE, sede)

        # ---- Movimientos para el Kardex ----
        MovimientoInventario.objects.create(
            producto=producto, sede=sede,
            tipo=MovimientoInventario.TIPO_IN, qty=100,
            referencia="CARGA-PRUEBA", nota="Carga inicial de prueba",
            usuario=almacen,
        ).aplicar()

        MovimientoInventario.objects.create(
            producto=producto, sede=sede,
            tipo=MovimientoInventario.TIPO_OUT, qty=20,
            referencia="SAL-PRUEBA-001", nota="Despacho a obra OBRA-PRUEBA-001",
            usuario=almacen,
        ).aplicar()

        MovimientoInventario.objects.create(
            producto=producto, sede=sede,
            tipo=MovimientoInventario.TIPO_ADJ, qty=-2,
            referencia="AJUSTE-PRUEBA", nota="Ajuste por inventario físico",
            usuario=almacen,
        ).aplicar()

        # ---- Obra para probar la cuadrilla ----
        proyecto, _ = Proyecto.objects.get_or_create(
            codigo="OBRA-PRUEBA-001",
            defaults={
                "nombre": "Obra de prueba",
                "sede": sede,
                "creado_por": almacen,
                "responsable": tecnico_resp,
                "estado": EstadoProyecto.EN_PROCESO,
            },
        )

        ProyectoMaterial.objects.get_or_create(
            proyecto=proyecto,
            producto=producto,
            defaults={"cantidad_planificada": 50, "cantidad_entregada": 20},
        )

        self.stdout.write(self.style.SUCCESS("Datos de prueba listos. Password de todos: test1234"))
        self.stdout.write(f"- Sede: {sede.nombre}")
        self.stdout.write(f"- Producto (para el Kardex): {producto.nombre}")
        self.stdout.write(f"- Usuario Almacén: {almacen.username}")
        self.stdout.write(f"- Usuario Técnico responsable de la obra: {tecnico_resp.username}")
        self.stdout.write(f"- Usuario Técnico de la cuadrilla (receptor): {tecnico_crew.username}")
        self.stdout.write(f"- Obra: {proyecto.codigo} (20 unidades de '{producto.nombre}' ya despachadas)")
        self.stdout.write(
            "\nPara probar la cuadrilla: entra como 'tecnico_resp_test', ve al detalle de "
            "'OBRA-PRUEBA-001' -> Asignar a Cuadrilla, y repártele unidades a 'tecnico_crew_test'. "
            "Luego entra como 'tecnico_crew_test' y revisa su dashboard."
        )
