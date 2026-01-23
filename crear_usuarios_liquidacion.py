#!/usr/bin/env python
"""
Script para crear usuarios de prueba para el sistema de liquidación
"""
import os
import sys

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.contrib.auth import get_user_model
from inventario.models import Sede, UserProfile

User = get_user_model()

def mostrar_estado_actual():
    """Mostrar sedes y usuarios actuales"""
    print("\n" + "="*60)
    print("ESTADO ACTUAL DEL SISTEMA")
    print("="*60)
    
    print("\n📍 SEDES REGISTRADAS:")
    sedes = Sede.objects.all().order_by('tipo', 'nombre')
    for sede in sedes:
        print(f"   ID {sede.id}: {sede.nombre} ({sede.tipo}) - Activa: {sede.activo}")
    
    print("\n👥 USUARIOS REGISTRADOS:")
    users = User.objects.all()
    for user in users:
        if hasattr(user, 'profile'):
            sede_nombre = user.profile.sede_principal.nombre if user.profile.sede_principal else "Sin sede"
            print(f"   {user.username}: Rol={user.profile.rol}, Sede={sede_nombre}")
        else:
            print(f"   {user.username}: SIN PERFIL")
    
    return sedes

def crear_usuarios_prueba(sedes):
    """Crear usuarios de prueba con diferentes roles"""
    print("\n" + "="*60)
    print("CREANDO USUARIOS DE PRUEBA")
    print("="*60)
    
    # Buscar sede central y secundarias
    sede_central = sedes.filter(tipo='CENTRAL').first()
    sede_secundaria = sedes.filter(tipo='SECUNDARIO').first()
    
    if not sede_central:
        print("❌ No se encontró una sede CENTRAL. Creando una...")
        sede_central = Sede.objects.create(
            nombre="SEDE-CENTRAL",
            tipo="CENTRAL",
            descripcion="Almacén central",
            activo=True
        )
        print(f"   ✅ Sede central creada: {sede_central.nombre}")
    
    if not sede_secundaria:
        print("❌ No se encontró una sede SECUNDARIA. Creando una...")
        sede_secundaria = Sede.objects.create(
            nombre="SEDE-SECUNDARIA",
            tipo="SECUNDARIO",
            descripcion="Almacén secundario",
            activo=True
        )
        print(f"   ✅ Sede secundaria creada: {sede_secundaria.nombre}")
    
    usuarios_a_crear = [
        {
            'username': 'almacen_central',
            'password': 'almacen123',
            'email': 'almacen_central@test.com',
            'rol': 'ALMACEN',
            'sede': sede_central,
            'descripcion': 'Usuario ALMACEN de sede CENTRAL (puede liquidar todo)'
        },
        {
            'username': 'almacen_sede',
            'password': 'almacen123',
            'email': 'almacen_sede@test.com',
            'rol': 'ALMACEN',
            'sede': sede_secundaria,
            'descripcion': 'Usuario ALMACEN de sede SECUNDARIA (solo puede liquidar su sede)'
        },
        {
            'username': 'admin_sistema',
            'password': 'admin123',
            'email': 'admin@test.com',
            'rol': 'ADMIN',
            'sede': sede_central,
            'is_staff': True,
            'is_superuser': True,
            'descripcion': 'Usuario ADMIN (acceso completo)'
        },
        {
            'username': 'jefa_global',
            'password': 'jefa123',
            'email': 'jefa@test.com',
            'rol': 'JEFA',
            'sede': sede_central,
            'descripcion': 'Usuario JEFA (acceso completo)'
        },
    ]
    
    print("\n🔧 Creando usuarios...")
    
    for datos in usuarios_a_crear:
        username = datos['username']
        
        try:
            # Verificar si ya existe
            if User.objects.filter(username=username).exists():
                user = User.objects.get(username=username)
                print(f"   ⚠️ {username} ya existe")
                
                # Actualizar o crear perfil
                profile, created = UserProfile.objects.get_or_create(
                    user=user,
                    defaults={
                        'rol': datos['rol'],
                        'sede_principal': datos['sede']
                    }
                )
                if not created:
                    profile.rol = datos['rol']
                    profile.sede_principal = datos['sede']
                    profile.save()
                print(f"      → Perfil {'creado' if created else 'actualizado'}: {datos['rol']} en {datos['sede'].nombre}")
            else:
                # Crear usuario
                user = User.objects.create_user(
                    username=username,
                    email=datos['email'],
                    password=datos['password'],
                    is_staff=datos.get('is_staff', False),
                    is_superuser=datos.get('is_superuser', False)
                )
                
                # Crear perfil
                UserProfile.objects.create(
                    user=user,
                    rol=datos['rol'],
                    sede_principal=datos['sede']
                )
                
                print(f"   ✅ {username} creado")
                print(f"      → Rol: {datos['rol']}")
                print(f"      → Sede: {datos['sede'].nombre}")
                print(f"      → {datos['descripcion']}")
        except Exception as e:
            print(f"   ❌ Error con {username}: {e}")
    
    return True

def mostrar_credenciales():
    """Mostrar credenciales de acceso"""
    print("\n" + "="*60)
    print("🔐 CREDENCIALES DE ACCESO")
    print("="*60)
    
    credenciales = [
        ("almacen_central", "almacen123", "ALMACEN", "CENTRAL", "Puede liquidar TODAS las sedes + central"),
        ("almacen_sede", "almacen123", "ALMACEN", "SECUNDARIA", "Solo puede liquidar SU SEDE"),
        ("admin_sistema", "admin123", "ADMIN", "CENTRAL", "Acceso completo a todo"),
        ("jefa_global", "jefa123", "JEFA", "CENTRAL", "Acceso completo a todo"),
    ]
    
    print("\n┌─────────────────┬─────────────┬─────────┬────────────┬──────────────────────────────────────┐")
    print("│ Usuario         │ Contraseña  │ Rol     │ Sede       │ Permisos Liquidación                 │")
    print("├─────────────────┼─────────────┼─────────┼────────────┼──────────────────────────────────────┤")
    
    for user, pwd, rol, sede, permisos in credenciales:
        print(f"│ {user:<15} │ {pwd:<11} │ {rol:<7} │ {sede:<10} │ {permisos:<36} │")
    
    print("└─────────────────┴─────────────┴─────────┴────────────┴──────────────────────────────────────┘")
    
    print("\n📋 INSTRUCCIONES:")
    print("   1. Accede a: http://127.0.0.1:8000/login/")
    print("   2. Inicia sesión con cualquiera de los usuarios de arriba")
    print("   3. Ve a: http://127.0.0.1:8000/liquidacion/")
    print("   4. Verás las opciones según tu rol y sede")
    
    print("\n⏰ NOTA: La liquidación solo está habilitada los LUNES")
    print("   Hoy puedes ver el dashboard pero los botones estarán deshabilitados")
    print("   si no es lunes.\n")

def main():
    print("\n🚀 CONFIGURACIÓN DE USUARIOS PARA SISTEMA DE LIQUIDACIÓN")
    print("="*60)
    
    try:
        # Mostrar estado actual
        sedes = mostrar_estado_actual()
        
        # Crear usuarios de prueba
        crear_usuarios_prueba(sedes)
        
        # Mostrar credenciales
        mostrar_credenciales()
        
        print("✅ Configuración completada exitosamente!\n")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        return 1
    
    return 0

if __name__ == "__main__":
    sys.exit(main())
