# Automatización RPA - Descarga y Extracción Facturas DIAN

## 1. ¿Qué hace este bot?
Este RPA automatiza la descarga de representaciones gráficas de facturas electrónicas desde el portal de la DIAN a partir de un listado de códigos CUFE. Posteriormente, extrae los datos de los ítems y la información del emisor desde los PDFs descargados, consolidando todo en un reporte CSV estructurado.

## 2. Flujo del Proceso
El proceso está dividido arquitectónicamente en dos fases para separar la recolección de datos de la transformación lógica:
```text
[ cufes.csv ] 
      │
      ▼
(Fase 1: Extracción Web / Playwright + 2Captcha) 
      │
      ▼
[ Carpeta PDFs locales ] 
      │
      ▼
(Fase 2: Procesamiento Local / pdfplumber + pandas) 
      │
      ▼
[ reporte_facturas.csv ]
```

## 3. Requisitos del Sistema
*   **Python:** 3.9 o superior.
*   **Navegador:** Chrome o Chromium instalado.
*   **Dependencias principales:** `playwright`, `pandas`, `pdfplumber`, `python-dotenv` y `2captcha-python`.
*   **Terceros:** Cuenta activa en 2Captcha con saldo disponible.
*   **Entorno Gráfico:** El script se ejecuta con `headless=False` (requiere pantalla). No funcionará en servidores sin interfaz gráfica (GUI) configurada.

## 4. Instalación
Clona el repositorio y configura tu entorno virtual:
```bash
python -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## 5. Configuración
1.  Crea un archivo `.env` en la raíz (puedes basarte en el `.env.example` provisto, el cual no contiene secretos).
    *   `TWOCAPTCHA_API_KEY`: Tu API key de 2Captcha.
    *   `CAPTCHA_MODE`: `2captcha` (automático) o `manual` (para intervención humana).
    *   `PDF_PASSWORD`: (Opcional) en caso de requerir descifrado de archivos.
2.  Prepara tu archivo de entrada `cufes.csv` en la carpeta raíz. Debe contener dos columnas separadas por coma: `cufe,nit`.

## 6. Salidas del Proceso
Al finalizar, el bot genera los siguientes directorios y archivos:
*   `data/downloads/`: Almacena los PDFs nativos descargados desde la DIAN.
*   `_debug/`: Capturas de pantalla y logs de estado en caso de fallos (útil para auditoría).
*   `data/output/reporte_facturas.csv`: Archivo final. Contiene las columnas: *Número de factura, Fecha de emisión, NIT del emisor, Código, Descripción, Cantidad, Precio unitario.*

## 7. Decisiones Técnicas de Arquitectura
*   **Precarga de Tokens y Paralelismo:** Se configuraron 8 hilos para precargar hasta 4 documentos en paralelo. Dado que los tokens de reCAPTCHA tienen un TTL (Time To Live) de 240 segundos, esto elimina el tiempo muerto de espera inyectando tokens válidos justo cuando el bot los necesita.
*   **Tolerancia a Fallos (Retries y Timeouts):** Se maneja un máximo de 3 reintentos por CUFE con un timeout de 30s. La filosofía es *fail-fast* (fallar rápido): si el portal de la DIAN no responde, es mejor abortar ese ítem, liberar memoria y reintentar, en lugar de bloquear el hilo principal.
*   **Pausas Aleatorias:** Se inyectaron pausas de 1.5 a 3.0 segundos para simular comportamiento humano (evadir el Rate Limiting) y proteger la IP del bot de baneos por el WAF del gobierno.
*   **Plan B (Captcha Manual):** Se integró una modalidad de fallback manual por si el servicio de 2Captcha sufre una caída, garantizando la continuidad operativa de la extracción.

## 8. Reporte de Tiempos y Restricciones Métricas
El proceso fue evaluado con una carga de 10 CUFEs. A continuación, el reporte de telemetría y el análisis de restricciones del negocio:

```text
------------------------------------------------------------
RESUMEN

Tiempos
  fase 1 (descarga):  167.0 s (98%)
  fase 2 (leer pdfs): 3.0 s (2%)
  total:              170.0 s

Descarga: 10 facturas, 10 ok, 0 con fallo
  por factura: prom 14.0 s, min 4.0 s, max 90.1 s, desv 26.8 s
  bien al primer intento: 9/10 (90%)
  intentos en total: 11 (1.10 por factura)
  esperando a 2captcha: 52.0 s (31% de la fase 1)
  pausas entre facturas: 20.6 s (12% de la fase 1)

PDFs
  leídos: 10 (21 productos en total)
  por pdf: prom 0.297 s, min 0.258 s, max 0.367 s
  velocidad: 3.35 pdfs por segundo
  pdfs sin productos: 0

Configuración usada
  timeouts: descarga 30 s, diálogo 10 s, resultado 30 s
  2captcha: 8 hilos, precarga de 4 facturas, token válido 240 s
  pausa entre facturas: 1.5 a 3.0 s
  máximo 3 intentos por factura
------------------------------------------------------------
```
**Análisis de Restricciones del Proceso:**
1.  **Cuello de botella en terceros:** El 98% del tiempo operativo ocurre en la descarga. De ese tiempo, el 31% se pierde esperando la resolución del Captcha. La velocidad del bot está dictada por el proveedor externo, no por el poder de cómputo local (la extracción en Pandas toma solo 3 segundos en total).
2.  **Inestabilidad de Origen:** La variación en la latencia del portal de la DIAN (min 4.0s a max 90.1s) valida la necesidad del sistema de reintentos lógicos implementado, logrando recuperar y procesar con éxito el 10% de transacciones que fallaron en el primer intento (First-Pass Yield: 90%).

## 9. Idempotencia y Eficiencia Operativa
El script es idempotente: si ocurre una interrupción por pérdida de red o caída de la página, al volver a ejecutar el proceso, el sistema validará el directorio `data/downloads/` y omitirá (saltará) de forma automática los PDFs que ya fueron descargados, reanudando desde donde falló sin incurrir en reprocesos ni costos adicionales.

## 10. Costo y Seguridad
*   **Consumo de API:** Cada CUFE procesado consume exactamente 2 tokens de 2Captcha (uno para la búsqueda inicial y otro para habilitar la descarga). El costo variable debe escalarse en función de este consumo.
*   **Seguridad:** Las credenciales operativas nunca deben ser expuestas. Asegúrese de que el archivo `.env` permanezca excluido del control de versiones (el archivo `.gitignore` ya está configurado para esto).

## 11. Limitaciones Conocidas
1.  **Dependencia Visual:** El *parser* (pdfplumber) confía en la maquetación visual (layout) y el texto nativo del PDF de la DIAN. Si la DIAN emite PDFs como imágenes planas o altera el nombre de las columnas (Regex), la Fase 2 fallará y requerirá mantenimiento.
2.  **Validación de Datos:** En la extracción de la tabla de productos, las filas donde la columna "Cantidad" no contenga un valor numérico serán descartadas para evitar contaminación en el dataset final.
3.  **Cambio de Sitekey:** Si la DIAN actualiza el `sitekey` de su reCAPTCHA de manera silenciosa, el script abortará la operación intencionalmente por seguridad.

## 12. Troubleshooting (Solución de problemas)
*   Si el bot falla sistemáticamente, revise el directorio `_debug/`. Encontrará capturas de pantalla del estado exacto del navegador al momento de la excepción (ej. "Portal en mantenimiento").
*   Si el proceso se queda congelado en la inyección del CUFE, verifique su saldo de 2Captcha o cambie `CAPTCHA_MODE=manual` en su `.env` para desbloquear el lote pendiente.