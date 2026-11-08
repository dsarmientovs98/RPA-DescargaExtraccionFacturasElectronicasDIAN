# Automatización RPA - Descarga y Extracción Facturas DIAN

## 1. ¿Qué hace este bot?
Este RPA automatiza la descarga de representaciones gráficas de facturas electrónicas desde el portal de la DIAN a partir de un listado de códigos CUFE. Posteriormente, extrae los datos de los ítems y la información del emisor desde los PDFs descargados, consolidando todo en un reporte CSV estructurado.

## 2. Flujo del Proceso
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

## 4. Instalación
```bash
python -m venv venv
source venv/bin/activate  # En Windows: venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
```

## 6. Salidas del Proceso
*   `data/downloads/`: Almacena los PDFs nativos descargados desde la DIAN.
*   `_debug/`: Capturas de pantalla y logs de estado en caso de fallos (útil para auditoría).
*   `data/output/reporte_facturas.csv`: Archivo final. Contiene las columnas: *Número de factura, Fecha de emisión, NIT del emisor, Código, Descripción, Cantidad, Precio unitario.*

## 7. Enfoque
*   **Precarga de Tokens y Paralelismo:** 8 hilos para precargar hasta 4 documentos en paralelo. Dado que los tokens de reCAPTCHA tienen un TTL (Time To Live) de 240 segundos, esto elimina el tiempo muerto de espera inyectando tokens válidos justo cuando el bot los necesita.
*   **Tolerancia a Fallos (Retries y Timeouts):**  máximo 3 reintentos por CUFE con un timeout de 30s. La filosofía es *fail-fast* (fallar rápido): si el portal de la DIAN no responde, es mejor abortar ese ítem, liberar memoria y reintentar, en lugar de bloquear el hilo principal.
*   **Pausas Aleatorias:**  pausas de 1.5 a 3.0 segundos para simular comportamiento humano (evadir el Rate Limiting) y proteger la IP del bot de baneos por el WAF del gobierno.
*   **Plan B (Captcha Manual):**  modalidad de fallback manual por si el servicio de 2Captcha sufre una caída, garantizando la continuidad operativa de la extracción.

## 8. Reporte de Tiempos y Restricciones Métricas

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

