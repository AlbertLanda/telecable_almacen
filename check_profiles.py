import os
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'config.settings')

import django
django.setup()

from django.contrib.auth import get_user_model
from inventario.models import UserProfile

User = get_user_model()

print("Verificación de perfiles de usuario:")
print("="*50)

users = User.objects.all()
for u in users:
    has_profile = hasattr(u, 'profile')
    if has_profile and u.profile:
        print(f"✅ {u.username}: TIENE perfil (ID: {u.profile.id}, Rol: {u.profile.rol})")
    else:
        print(f"❌ {u.username}: SIN perfil")

print("\nTotal usuarios:", users.count())
print("Usuarios con perfil:", UserProfile.objects.count())
