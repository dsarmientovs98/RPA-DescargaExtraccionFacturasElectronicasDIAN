# Descarga y extracción de facturas electrónicas de la DIAN

Automatiza dos tareas sobre una lista de CUFEs:

1. **Fase 1 – Adquisición (RPA):** descarga el PDF de cada factura desde el catálogo VPFE de la DIAN, resolviendo el captcha Cloudflare Turnstile con 2Captcha.
2. **Fase 2 – Extracción:** lee los PDFs con `pdfplumber` y genera un CSV con los datos de la factura y sus líneas de producto.

Al final imprime un resumen con los **tiempos empleados y las restricciones métricas** del proceso (ver [Tiempos y restricciones métricas](#tiempos-y-restricciones-métricas)).

```
cufes.csv ──► Fase 1: Playwright + 2Captcha ──► data/downloads/<cufe>.pdf
                                                         │
resumen en consola ◄── data/output/reporte_facturas.csv ◄┘  Fase 2: pdfplumber
```

Flujo por CUFE: abre el formulario → llena CUFE y NIT → inyecta el token del captcha → *Buscar* → espera el resultado → resuelve el captcha de la pantalla de descarga → *Descargar PDF* (reingresa el NIT si la DIAN lo pide en un modal) → guarda `<cufe>.pdf`.

## Requisitos

- **Python 3.9+** (probado con 3.9 en macOS).
- **Cuenta de 2Captcha con saldo** (o usar el modo manual).
- **Dependencias:** `playwright`, `pandas`, `pdfplumber`, `python-dotenv` y `2captcha-python`, más `playwright install chromium` por si el equipo no tiene Chrome.
- **Pantalla:** el navegador abre con `headless=False`, así que en un servidor sin interfaz gráfica no corre.

## Instalación

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## Configuración

Todo se resuelve relativo a la carpeta de `main.py`; no hay rutas absolutas. Junto al script deben estar:

**`.env`** (copia `.env.example` y complétalo):

| Variable | Obligatoria | Descripción |
|---|---|---|
| `TWOCAPTCHA_API_KEY` | Sí, si `CAPTCHA_MODE=2captcha` | API key de 2Captcha |
| `CAPTCHA_MODE` | No | `2captcha` (por defecto) o `manual` |
| `PDF_PASSWORD` | No | Contraseña de los PDFs si vienen cifrados; si no, se abren normal |

**`cufes.csv`** con encabezado `cufe,nit`. El NIT es opcional; si falta se usa `900156264`.

```csv
cufe,nit
3a4b5c...e9f0,900156264
7d8e9f...a1b2,
```

## Uso

```bash
python main.py          # procesa todos los CUFEs de cufes.csv
python main.py --uno    # prueba solo con el primer CUFE
```

- **Modo manual** (`CAPTCHA_MODE=manual`): el navegador espera a que marques la casilla *"Verifique que es un ser humano"* en cada pantalla.
- **Reanudable:** si vuelves a correrlo, salta los CUFEs cuyo PDF ya existe en `data/downloads/`.

## Salidas

```
data/
├── downloads/
│   ├── <cufe>.pdf                  # un PDF por factura
│   └── _debug/                     # capturas y HTML cuando algo falla
└── output/
    └── reporte_facturas.csv        # una fila por línea de producto
```

Columnas de `reporte_facturas.csv`: `CUFE_ending` (últimos 4 caracteres del CUFE), `Numero_Factura`, `Fecha_Emision`, `NIT_Emisor`, `Codigo`, `Descripcion`, `Cantidad`, `Precio_Unitario`.

## Tiempos y restricciones métricas

El script mide y reporta lo siguiente al terminar. Corrida real con 10 CUFEs:

```
RESUMEN
------------------------------------------------------------

Tiempos
  fase 1 (descarga):     134.92 s  (98.0%)
  fase 2 (PDFs y CSV):     2.80 s  (2.0%)
  total:                 137.72 s

Fase 1
  10 CUFEs: 10 descargados, 0 ya estaban, 0 fallaron
  tiempo por CUFE: promedio 11.42 s, mín 3.82 s, máx 61.67 s, desv. 18.02 s
  a la primera (sin reintentos): 9 de 10 (90.0%)
  intentos en total: 11 (1.10 por CUFE)
  esperando a 2captcha: 34.07 s (25.2% de la fase 1)
  pausas entre CUFEs: 17.92 s (13.3% de la fase 1)

Fase 2
  10 PDFs leídos, 21 líneas de producto
  tiempo por PDF: promedio 0.279 s, mín 0.248 s, máx 0.351 s
  velocidad: 3.57 PDFs por segundo
  PDFs sin ningún ítem: 0

Configuración usada
  timeout de descarga: 30 s por intento
  timeout del diálogo del NIT: 10 s
  timeout del resultado de la búsqueda: 30 s
  2captcha: 8 hilos, precarga de 4 docs, el token dura 240 s
  pausa entre docs: entre 1.5 y 3.0 s (aleatoria)
  reintentos: máximo 3 por CUFE
```

**Cómo leerlo**

- **Cuello de botella:** la Fase 1 consume el 98 % del tiempo porque depende de la red, de la DIAN y del captcha. El parseo de PDFs es marginal (~0.28 s por archivo).
- **First-pass yield:** 9 de 10 documentos salieron sin reintentos. Los 11 intentos totales indican que un solo CUFE necesitó una segunda vuelta; el máximo de 61.67 s y la desviación alta (18.02 s) probablemente vienen de ese caso.
- **Espera de 2Captcha (25.2 %):** es tiempo *neto* de bloqueo. Como los tokens se piden por adelantado en paralelo, buena parte de la resolución ocurre mientras se procesan otros CUFEs.
- **Pausas (13.3 %):** retraso deliberado para no golpear la DIAN a ritmo de bot.

**Restricciones del proceso** (constantes al inicio de `main.py`):

| Restricción | Valor | Propósito |
|---|---|---|
| Timeout de descarga | 30 s por intento | Fallar rápido y reintentar |
| Timeout del diálogo del NIT | 10 s | Límite de espera del modal que pide el NIT |
| Timeout del resultado de búsqueda | 30 s | Límite de espera de la respuesta de la DIAN |
| Espera por token de 2Captcha | 120 s (consulta cada 5 s) | Evitar bloqueos indefinidos |
| Pool de 2Captcha | 8 hilos, precarga de 4 docs | Tener tokens listos antes de necesitarlos |
| Vigencia del token | 240 s | Descartar tokens vencidos y pedir otro |
| Pausa entre documentos | 1.5–3.0 s aleatoria | Reducir riesgo de bloqueo por ritmo |
| Reintentos | 3 por CUFE | Resiliencia ante fallos transitorios |

## Decisiones de diseño

- **Precarga de tokens en paralelo:** resolver un captcha toma segundos; pedirlo por adelantado evita que el navegador espere ocioso.
- **Reintentos con evidencia:** cada fallo guarda captura y HTML en `_debug/` para diagnosticar sin repetir la corrida.
- **Fallar de inmediato ante cambios de configuración:** si la DIAN cambia el `sitekey` de Turnstile, el script se detiene en lugar de gastar reintentos que no pueden funcionar.
- **Modo manual como plan B:** permite operar sin 2Captcha resolviendo el captcha a mano.
- **Fallback de navegador:** usa Chrome si está instalado; si no, el Chromium de Playwright.

## Limitaciones conocidas

- **El parser depende del formato del PDF:** busca las etiquetas `Número de Factura:`, `Fecha de Emisión:` y `Nit del Emisor:`, y una tabla con las columnas código, descripción, cantidad y precio unitario. Un formato distinto produce `NO_ENCONTRADO` o ninguna línea de producto.
- **La Fase 2 procesa todos los PDFs** de `data/downloads/`, no solo los del `cufes.csv` actual.
- **Cantidad y precio se guardan como texto**, tal cual aparecen en el PDF (sin convertir a número).
- Las filas cuya cantidad no es numérica (totales, notas) se descartan.
- Depende de la disponibilidad y del diseño del sitio de la DIAN y del servicio de 2Captcha.
- Requiere entorno gráfico (`headless=False`).

## Solución de problemas

| Síntoma | Qué hacer |
|---|---|
| `Falta TWOCAPTCHA_API_KEY` | Crea el `.env` o usa `CAPTCHA_MODE=manual` |
| `Crea cufes.csv con columnas: cufe,nit` | Agrega el archivo junto a `main.py` |
| `No cargó el formulario` | Revisa `data/downloads/_debug/01_sin_formulario.png` y tu conexión |
| `La DIAN cambió el sitekey` | Actualiza `TURNSTILE_SITEKEY` en `main.py` |
| `Bloqueado por controles de seguridad` o `tiempo agotado` | Se reintenta solo; si persiste, usa modo manual o aumenta `PAUSA_ENTRE_DOCS_S` |
| Un PDF sale con 0 ítems | El formato difiere; revisa `encontrar_indices_columnas` y `extraer_datos_pdf` |
| No encuentra Chrome | Corre `playwright install chromium` |

Para depurar más a fondo, activa `VOLCAR_PRIMER_RESULTADO` o `DEPURAR_POST` en `main.py`.

## Costo y seguridad

- Cada CUFE usa **hasta 2 tokens de 2Captcha** (búsqueda y descarga), más los de cualquier reintento. Con 10 CUFEs el consumo es mínimo, pero escala con el lote.
- **No subas el `.env` al repositorio.** Está incluido en `.gitignore`.

## Mejoras futuras

- Pruebas automáticas del parser con PDFs de ejemplo.
- Logging a archivo en lugar de solo consola.
- Modo headless y contenedor Docker.
- Convertir cantidad y precio a tipos numéricos en el CSV.
