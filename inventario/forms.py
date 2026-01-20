from django import forms
from django.contrib.auth.models import User
from .models import UserProfile

class UsuarioSedeForm(forms.Form):
    username = forms.CharField(label="Usuario", max_length=150, widget=forms.TextInput(attrs={'class': 'form-control uppercase-input'}))
    first_name = forms.CharField(label="Nombres", max_length=100, widget=forms.TextInput(attrs={'class': 'form-control uppercase-input'}))
    last_name = forms.CharField(label="Apellidos", max_length=100, widget=forms.TextInput(attrs={'class': 'form-control uppercase-input'}))
    password = forms.CharField(label="Contraseña", required=False, widget=forms.PasswordInput(attrs={'class': 'form-control', 'placeholder': 'Dejar vacío para no cambiar'}))
    
    rol = forms.ChoiceField(
        label="Rol",
        choices=UserProfile.Rol.choices,
        widget=forms.Select(attrs={'class': 'form-control'})
    )

    def clean_username(self):
        username = self.cleaned_data['username'].upper()
        # Validar duplicados solo al crear (lógica simple)
        return username