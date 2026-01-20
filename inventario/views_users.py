from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.contrib.auth.models import User
from .models import UserProfile, Sede
from .views_dashboard import _require_roles

@login_required
def usuarios_list(request):
    """Lista usuarios ACTIVOS y permite filtrar por Sede."""
    profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)
    
    # 1. Sedes disponibles
    sedes_disponibles = profile.sedes_permitidas.all().order_by('id')
    if not sedes_disponibles.exists():
        sedes_disponibles = Sede.objects.filter(id=profile.sede_principal.id)

    # 2. Sede Activa
    sede_id_param = request.GET.get('sede_id')
    sede_activa = profile.get_sede_operativa()

    if sede_id_param:
        try:
            sede_solicitada = Sede.objects.get(id=sede_id_param)
            if sede_solicitada in sedes_disponibles:
                sede_activa = sede_solicitada
        except Sede.DoesNotExist:
            pass

    # 3. Filtro
    usuarios = UserProfile.objects.filter(
        sede_principal=sede_activa,
        user__is_active=True 
    ).exclude(user=request.user).select_related('user').order_by('user__username')

    return render(request, 'inventario/usuarios_list.html', {
        'usuarios': usuarios,
        'sede_activa': sede_activa,
        'sedes_disponibles': sedes_disponibles,
    })

@login_required
def usuario_edit(request, user_id):
    """Edición simplificada: Solo Rol y Sede (sin contraseñas)."""
    admin_profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)
    target_profile = get_object_or_404(UserProfile, user_id=user_id)
    user = target_profile.user

    # Seguridad
    if target_profile.sede_principal not in admin_profile.sedes_permitidas.all():
        messages.error(request, "No puedes editar usuarios de otras sedes.")
        return redirect('usuarios_list')

    if request.method == 'POST':
        new_username = request.POST.get('username', '').strip().upper()
        new_rol = request.POST.get('rol')
        new_sede_id = request.POST.get('sede_id')

        # 1. Cambiar Username
        if new_username and new_username != user.username:
            if User.objects.filter(username=new_username).exclude(id=user.id).exists():
                messages.error(request, "El usuario ya está en uso.")
                return redirect('usuarios_list')
            user.username = new_username
            user.save()

        # 2. Cambiar Rol
        if new_rol:
            target_profile.rol = new_rol

        # 3. Cambiar Sede (Mover usuario)
        if new_sede_id:
            new_sede = get_object_or_404(Sede, id=new_sede_id)
            if new_sede in admin_profile.sedes_permitidas.all():
                target_profile.sede_principal = new_sede
                target_profile.sedes_permitidas.add(new_sede)
            
        target_profile.save()
        messages.success(request, "Usuario actualizado correctamente.")
    
    return redirect(f"/usuarios/?sede_id={target_profile.sede_principal.id}")

@login_required
def usuario_delete(request, user_id):
    """Soft delete (desactivar)."""
    admin_profile = _require_roles(request.user, UserProfile.Rol.ADMIN, UserProfile.Rol.JEFA)
    target_profile = get_object_or_404(UserProfile, user_id=user_id)
    
    if target_profile.sede_principal not in admin_profile.sedes_permitidas.all():
        messages.error(request, "Sin permiso.")
        return redirect('usuarios_list')
    
    sede_redirect = target_profile.sede_principal.id
    
    user = target_profile.user
    user.is_active = False 
    user.save()
    
    messages.success(request, "Usuario desactivado.")
    return redirect(f"/usuarios/?sede_id={sede_redirect}")