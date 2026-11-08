# Documento Técnico: Entorno de Automatización en VS Code
Registro completo de la arquitectura y configuraciones.

## 1. Herramientas Globales y de Sistema
* **Motor de Python (`uv`):** Se instaló globalmente `uv` (basado en Rust) para reemplazar las herramientas clásicas de Python (`pip` y `venv`). Esto garantiza la creación de entornos y descarga de paquetes en milisegundos, optimizando la memoria de tu Mac.
* **Integración con Terminal:** Se instaló el comando `code` en el PATH de macOS, permitiendo abrir Visual Studio Code directamente desde cualquier ubicación en la terminal mediante `code .`.

## 2. Arquitectura de Carpetas (Estándar ETL)
El proyecto se estructuró separando la lógica del código de los datos para garantizar escalabilidad:
* `PruebaTecnica/` (Raíz del proyecto)
  * `data/input/`: Destino de los archivos crudos (Excel/CSV) a procesar.
  * `data/output/`: Destino de los reportes o bases de datos limpias generadas por el script.
  * `data/archive/`: Destino de los archivos crudos una vez procesados (movidos vía script).

## 3. Entorno Virtual y Dependencias
* **Aislamiento:** Se creó un entorno virtual local (`.venv`) usando el comando `uv venv`.
* **Activación:** Se activa mediante `source .venv/bin/activate` (y VS Code lo detecta automáticamente al abrir su terminal integrada).
* **Librerías instaladas:** Se instalaron las herramientas base para manejo de datos en Excel usando `uv pip install pandas openpyxl`.
* **Reproducibilidad:** Se congelaron las versiones exactas en un archivo mediante `uv pip freeze > requirements.txt` para garantizar que el evaluador ejecute exactamente el mismo código.
* **Seguridad del repositorio:** Se creó un archivo `.gitignore` para evitar subir el entorno virtual (`.venv/`), archivos ocultos de Mac (`.DS_Store`) y la memoria caché de Python (`__pycache__/`).

## 4. Automatización del Editor (Ruff)
Visual Studio Code se configuró como una herramienta activa que corrige el código por ti al presionar guardar (`Cmd + S`).
* **Extensión principal:** Ruff de Astral Software (`astral-sh.ruff`). Un linter ultrarrápido que reemplaza a múltiples herramientas antiguas.
* **Configuración del espacio de trabajo (`.vscode/settings.json`):**

```json
{
    "python.defaultInterpreterPath": "${workspaceFolder}/.venv/bin/python",
    "editor.formatOnSave": true,
    "editor.codeActionsOnSave": {
        "source.fixAll.ruff": "explicit",
        "source.organizeImports.ruff": "explicit"
    },
    "python.analysis.typeCheckingMode": "basic",
    "[python]": {
        "editor.defaultFormatter": "astral-sh.ruff"
    }
}