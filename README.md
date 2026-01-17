# 📦 Sistema de Gestión de Almacén – Telecable

Sistema web para la **gestión de inventarios de almacén** de la empresa **Telecable**, orientado al control de materiales por sedes, registro de retiros y devoluciones, manejo de mermas, control de costos y trazabilidad histórica.

El sistema está diseñado para **escalar progresivamente**, iniciando con control desde almacén y proyectándose a futuro con reconocimiento de técnicos mediante cámara y lector de códigos de barras.

---

## 🎯 Objetivo del Sistema

- Controlar el **stock de materiales** por sede.
- Registrar **retiros y devoluciones** de materiales.
- Gestionar **mermas** y pérdidas.
- Calcular **costos por proyecto / centro de costo**.
- Mantener **historial completo** de movimientos.
- Alertar cuando un producto llegue a **stock mínimo** para reabastecimiento.
- Facilitar auditorías y toma de decisiones.

---

## 🧱 Arquitectura

El sistema sigue una **Arquitectura por Capas**, separando responsabilidades para mejorar mantenibilidad, escalabilidad y trabajo en equipo.

### 📂 Estructura del Proyecto

      telecable_almacen/
```bash
│
├── config/ # Configuración general del proyecto Django
│
├── inventario/ # Aplicación principal
│ ├── domain/ # Reglas de negocio y excepciones
│ ├── repositories/ # Acceso a datos (ORM / consultas)
│ ├── services/ # Casos de uso y lógica de aplicación
│ ├── management/ # Comandos personalizados (seed, utilidades)
│ ├── migrations/ # Migraciones de base de datos
│ ├── templates/
│ │ └── inventario/ # Vistas HTML
│ ├── static/
│ │ └── inventario/ # CSS y JS
│ ├── models.py # Modelos de datos
│ ├── views.py # Controladores
│ ├── urls.py # Rutas del módulo
│
├── manage.py
├── requirements.txt
├── README.md

```
---

## ⚙️ Tecnologías Utilizadas

- **Backend:** Django 4.2.11 (Python)
- **Lenguaje:** Python 3.11.9 (Python)
- **Frontend:** HTML, CSS, JavaScript
- **Base de Datos:** PostgreSQL15.15
- **Control de Versiones:** Git 2.52.0 + GitHub
- **Arquitectura:** Capas (Domain, Services, Repositories)
- **Entorno:** Virtualenv

---

## 🔁 Flujo General del Sistema

1. El encargado de almacén registra la **salida de materiales**.
2. El sistema guarda:
   - Quién retira
   - Fecha y hora
   - Sede
   - Proyecto / centro de costo
3. Al finalizar el trabajo:
   - Se registra la **devolución**
   - Se clasifica el material:
     - Reutilizable → vuelve a stock
     - No reutilizable → merma
4. El sistema calcula:
   - Costos reales
   - Pérdidas
   - Historial por técnico, sede o proyecto
5. Si el stock llega al mínimo:
   - Se genera alerta para **reabastecimiento por proveedor**

---

## 🔀 Flujo de Trabajo con Git

1. Cada integrante trabaja en su **branch**

feature/nombre-funcionalidad

2. Se realizan commits claros.
3. Se crea un **Pull Request**.
4. El líder técnico revisa y aprueba.
5. Se integra a `main`.

---

## ▶️ Instalación y Ejecución

```bash

Primeros codigos:

git clone https://github.com/AlbertLanda/telecable-almacen.git
cd telecable-almacen
python -m venv venv

```

Antes de ejecutar el sistema, cada integrante del equipo debe crear su archivo .env en la raíz del proyecto (telecable_almacen/.env).

Archivo .env

```bash
# Django
SECRET_KEY=django-insecure-cambia-esto-en-produccion
DEBUG=True

# Base de datos PostgreSQL
DB_NAME=telecable_almacen
DB_USER=postgres
DB_PASSWORD=tu_password
DB_HOST=localhost
DB_PORT=5432
```

⚠️ Importante:

El archivo .env NO se sube a GitHub (está en .gitignore).

Cada integrante debe usar sus propias credenciales locales.

Si no existe este archivo, el sistema no levantará correctamente.

```bash

Continuamos instalando estos codigos

.\venv\Scripts\activate
pip install -r requirements.txt
python manage.py migrate
python manage.py runserver

```
Acceder en:

http://127.0.0.1:8000/

Cómo subir tu avance al repositorio (Git)

1️⃣ Crear o usar tu rama de trabajo

```bash
git checkout -b feature/nombre-funcionalidad
```

2️⃣ Ver cambios realizados
```bash
git status
```
3️⃣ Agregar archivos modificados
```bash
git add .
```
4️⃣ Hacer commit (mensaje claro)
```bash
git commit -m "feat: registro de movimientos de almacén"
```
5️⃣ Subir cambios a GitHub
```bash
git push -u origin feature/nombre-funcionalidad
```

⚠️ Nadie hace push directo a main.
