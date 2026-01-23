# 🏢 Sistema de Liquidación Semanal

## 📋 Descripción

Sistema completo de liquidación semanal de inventario para Telecable, integrado con Django. Permite realizar liquidaciones de sedes individuales y del almacén central con control de acceso basado en roles y restricción por día de la semana.

## ✨ Características

### 🎯 **Control de Acceso por Rol**
- **ALMACEN (Sede Secundaria):** Solo puede liquidar su propia sede
- **ALMACEN (Sede Central):** Puede liquidar todas las sedes + almacén central
- **ADMIN:** Acceso completo a todas las liquidaciones
- **JEFA:** Acceso completo a todas las liquidaciones
- **SOLICITANTE:** Sin acceso a liquidación

### ⏰ **Restricción Temporal**
- **Permitido:** Sábado, Domingo y Lunes (fin de semana)
- **Bloqueado:** Martes a Viernes
- **Semana a liquidar:** Siempre la semana anterior

### 📊 **Funcionalidades**
- Dashboard con diseño oscuro consistente
- Liquidación de sedes individuales
- Liquidación del almacén central (verificación de consistencia)
- Lista paginada de liquidaciones con filtros
- Exportación a Excel
- Historial completo con estados

## 🔐 **Credenciales de Acceso**

| Usuario | Contraseña | Rol | Sede | Permisos |
|---------|------------|-----|------|----------|
| `almacen_central` | `almacen123` | ALMACEN | SEDE-JAUJA (CENTRAL) | ✅ Todas las sedes + central |
| `almacen_sede` | `almacen123` | ALMACEN | SEDE-HUANCAYO | ⚠️ Solo su sede |
| `admin_sistema` | `admin123` | ADMIN | SEDE-JAUJA | ✅ Acceso completo |
| `jefa_global` | `jefa123` | JEFA | SEDE-JAUJA | ✅ Acceso completo |
| `ALBERTLANDA` | (su contraseña) | ALMACEN | SEDE-JAUJA | ✅ Todas las sedes + central |
| `admin_almacen` | (su contraseña) | ADMIN | SEDE-JAUJA | ✅ Acceso completo |

## 🚀 **Instalación y Configuración**

### 1. **Requisitos**
- Python 3.12+
- Django 4.2+
- PostgreSQL (Ne recomendado)

### 2. **Configuración de la Base de Datos**
```bash
# Crear archivo .env
DB_NAME=telecable_db
DB_USER=postgres
DB_PASSWORD=your_password
DB_HOST=ep-xxx.us-east-2.aws.neon.tech
DB_PORT=5432
SECRET_KEY=your-secret-key
DEBUG=True
ALLOWED_HOSTS=localhost,127.0.0.1
```

### 3. **Migraciones y Configuración**
```bash
# Aplicar migraciones de liquidación
python manage.py migrate inventario

# Crear funciones SQL de liquidación
python setup_liquidacion_simple.py

# Crear usuarios de prueba
python crear_usuarios_liquidacion.py
```

### 4. **Iniciar el Servidor**
```bash
python manage.py runserver
```

## 📁 **Estructura de Archivos**

```
inventario/
├── models_liquidacion.py          # Modelos de liquidación
├── views_liquidacion.py           # Vistas de liquidación
├── services/
│   └── liquidacion_service.py     # Lógica de negocio
├── templates/inventario/liquidacion/
│   ├── dashboard_dark.html         # Dashboard principal
│   ├── lista.html                 # Lista de liquidaciones
│   ├── liquidar_sede.html         # Formulario liquidación sede
│   └── liquidar_central.html      # Formulario liquidación central
├── admin_liquidacion.py           # Configuración de admin
└── migrations/
    └── liquidacion_initial.py     # Migración inicial
```

## 🎨 **Diseño y UI**

- **Tema:** Oscuro consistente con el dashboard principal
- **Framework:** Tailwind CSS + Boxicons
- **Layout:** Sidebar + contenido principal
- **Responsive:** Adaptable a móviles y tablets

## 🔄 **Flujo de Trabajo**

### 1. **Acceso al Sistema**
1. Iniciar sesión con credenciales válidas
2. El menú muestra opciones según rol del usuario
3. Acceder a "📊 Liquidación" desde el sidebar

### 2. **Dashboard de Liquidación**
- Muestra KPIs de sedes y estado
- Alerta de día permitido/bloqueado
- Tarjetas de sedes con estado y acciones
- Historial reciente de liquidaciones

### 3. **Ejecución de Liquidación**
- **Sábado a Lunes:** Botones habilitados
- **Martes a Viernes:** Botones deshabilitados con mensaje
- Se liquida siempre la semana anterior
- Proceso con confirmación y mensajes de resultado

## 📊 **Estados de Liquidación**

| Estado | Descripción | Color |
|--------|-------------|-------|
| `PENDIENTE` | No liquidada | Amarillo |
| `LIQUIDADO` | Liquidación completada | Verde |
| `CONSISTENTE` | Verificación consistente | Verde |
| `INCONSISTENTE` | Se detectaron diferencias | Rojo |
| `REVISAR` | Requiere revisión manual | Amarillo |

## 🔧 **Configuración Avanzada**

### Modificar Días Permitidos
En `views_liquidacion.py`, función `puede_liquidar_hoy()`:
```python
# Cambiar días permitidos (0=Lunes, 5=Sábado, 6=Domingo)
if dia_semana in [5, 6, 0]:  # Sábado, Domingo, Lunes
```

### Personalizar Mensajes
En `views_liquidacion.py`:
```python
return True, "Hoy es sábado, puede realizar la liquidación."
```

### Agregar Nuevos Roles
En `models.py`, clase `UserProfile`:
```python
class Rol(models.TextChoices):
    NUEVO_ROL = "NUEVO", "Nuevo Rol"
```

## 🐛 **Troubleshooting**

### Error 403 Forbidden
- Verificar que el usuario tenga perfil UserProfile
- Ejecutar `python fix_missing_profiles.py`
- Revisar roles y permisos

### Error de Template
- Verificar que existan los templates en `inventario/templates/inventario/liquidacion/`
- Revisar nombres de campos en el modelo

### Funciones SQL no encontradas
- Ejecutar `python setup_liquidacion_simple.py`
- Verificar conexión a la base de datos

## 📝 **Notas de Desarrollo**

- El sistema usa funciones SQL almacenadas para procesamiento eficiente
- Las liquidaciones se almacenan con auditoría completa
- Se implementa logging de todas las operaciones
- El diseño es responsive y accesible

## 🤝 **Contribuciones**

1. Hacer fork del proyecto
2. Crear branch para la feature: `git checkout -b feature/nueva-funcionalidad`
3. Commits descriptivos
4. Push al branch: `git push origin feature/nueva-funcionalidad`
5. Crear Pull Request

## 📄 **Licencia**

Proyecto interno de Telecable - Todos los derechos reservados

---

## 📞 **Soporte**

Para problemas o preguntas:
- Revisar los logs del servidor Django
- Verificar configuración en `settings.py`
- Validar conexión a la base de datos
- Ejecutar scripts de diagnóstico en la raíz del proyecto
