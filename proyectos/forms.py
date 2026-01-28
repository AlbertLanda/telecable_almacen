from django import forms
from .models import Proyecto, ProyectoMaterial
from inventario.models import UserProfile
from django.contrib.auth.models import User

class ProyectoForm(forms.ModelForm):
    class Meta:
        model = Proyecto
        # ✅ CAMBIO: Quitamos 'codigo' y 'centro_costo' de aquí
        fields = ['nombre', 'sede', 'responsable', 'plano', 'inicio', 'fin', 'descripcion']
        
        widgets = {
            'inicio': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'fin': forms.DateInput(attrs={'type': 'date', 'class': 'form-control'}),
            'descripcion': forms.Textarea(attrs={'rows': 3, 'class': 'form-control', 'placeholder': 'Detalles técnicos...'}),
            'nombre': forms.TextInput(attrs={'class': 'form-control', 'placeholder': 'Ej: Instalación Fibra Óptica Sector Sur'}),
            'plano': forms.FileInput(attrs={'class': 'form-control'}),
            'sede': forms.Select(attrs={'class': 'form-select'}),
            'responsable': forms.Select(attrs={'class': 'form-select'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Filtramos para que solo salgan Técnicos
        tecnicos_ids = UserProfile.objects.filter(rol=UserProfile.Rol.SOLICITANTE).values_list('user_id', flat=True)
        self.fields['responsable'].queryset = User.objects.filter(id__in=tecnicos_ids)
        self.fields['responsable'].empty_label = "Seleccione un Técnico..."

class ProyectoMaterialForm(forms.ModelForm):
    class Meta:
        model = ProyectoMaterial
        fields = ['producto', 'cantidad_planificada']
        widgets = {
            'producto': forms.Select(attrs={'class': 'form-select'}),
            'cantidad_planificada': forms.NumberInput(attrs={'class': 'form-control', 'min': '1'}),
        }
        labels = {
            'cantidad_planificada': 'Cantidad a Usar'
        }