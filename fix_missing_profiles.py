import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.contrib.auth import get_user_model
from inventario.models import UserProfile, Sede

User = get_user_model()

print("Creando perfiles para usuarios que no tienen...")
print("="*50)

# Usuarios sin perfil
users_without_profile = []
for user in User.objects.all():
    if not hasattr(user, 'profile') or not user.profile:
        users_without_profile.append(user)

if not users_without_profile:
    print("✅ Todos los usuarios ya tienen perfil")
else:
    print(f"Se encontraron {len(users_without_profile)} usuarios sin perfil:")
    
    # Obtener la primera sede (sede central)
    sede_central = Sede.objects.filter(tipo='CENTRAL').first()
    if not sede_central:
        print("❌ No se encontró una sede central")
        exit(1)
    
    print(f"Usando sede central: {sede_central.nombre}")
    
    for user in users_without_profile:
        print(f"\n🔧 Creando perfil para: {user.username}")
        
        # Crear perfil con rol ALMACEN por defecto
        profile = UserProfile.objects.create(
            user=user,
            rol='ALMACEN',  # Rol por defecto
            sede_principal=sede_central,
        )
        
        print(f"   ✅ Perfil creado (ID: {profile.id}, Rol: {profile.rol}, Sede: {sede_central.nombre})")

print("\n" + "="*50)
print("✅ Proceso completado")

# Verificación final
print("\nVerificación final:")
total_users = User.objects.count()
total_profiles = UserProfile.objects.count()
print(f"Total usuarios: {total_users}")
print(f"Total perfiles: {total_profiles}")

if total_users == total_profiles:
    print("🎉 Todos los usuarios ahora tienen perfil!")
else:
    print(f"⚠️ Aún faltan {total_users - total_profiles} perfiles")
