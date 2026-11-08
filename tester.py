import argparse
import csv
import os
import random
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs

import pandas as pd
import pdfplumber
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright
from twocaptcha import TwoCaptcha

load_dotenv()
CAPTCHA_MODE = os.getenv("CAPTCHA_MODE", "2captcha").strip().lower()
API_KEY = os.getenv("TWOCAPTCHA_API_KEY")
PASSWORD = os.getenv("PDF_PASSWORD")

if CAPTCHA_MODE == "2captcha" and not API_KEY:
    raise ValueError("Falta TWOCAPTCHA_API_KEY en el .env (o usa CAPTCHA_MODE=manual)")

# --- sitio de la DIAN ---
NIT_DEFECTO = "900156264"
URL_DIAN = "https://catalogo-vpfe.dian.gov.co/User/SearchDocument"
URL_RESULTADO = "https://catalogo-vpfe.dian.gov.co/Document/ShowDocumentToPublic"
TURNSTILE_SITEKEY = "0x4AAAAAAAg1WuNb-OnOa76z"

# hay captcha en dos pantallas: la de búsqueda y la de resultado/descarga
URL_POR_ETAPA = {"busqueda": URL_DIAN, "descarga": URL_RESULTADO}

# --- timeouts de playwright (ms) ---
TIMEOUT_FORMULARIO_MS = 20_000
TIMEOUT_CAMPO_NIT_MS = 5_000
TIMEOUT_DIALOGO_MS = 10_000
TIMEOUT_DESCARGA_MS = 30_000
TIMEOUT_CAPTCHA_MANUAL_MS = 120_000
INTERVALO_SONDEO_MS = 200

# --- timeouts y pausas propias (s) ---
TIMEOUT_RESULTADO_S = 30
PAUSA_ENTRE_DOCS_S = (1.5, 3.0)

# --- 2captcha ---
TIMEOUT_2CAPTCHA_S = 120
POLLING_2CAPTCHA_S = 5
TOKEN_TTL_S = 240  # pasado este tiempo el token ya no sirve
VENTANA_PRECARGA = 4
MAX_RESOLUCIONES_PARALELAS = 8

MAX_INTENTOS = 3
VOLCAR_PRIMER_RESULTADO = False
DEPURAR_POST = False

# --- rutas ---
BASE_DIR = Path(
    "/Users/danielsarmientovanegas/Desktop/Auto/Analista_Innovacion/PruebaTecnica"
)
if not BASE_DIR.exists():
    BASE_DIR = Path(__file__).parent

CSV_ENTRADA = BASE_DIR / "cufes.csv"
DOWNLOADS_DIR = BASE_DIR / "data" / "downloads"
DEBUG_DIR = DOWNLOADS_DIR / "_debug"
OUTPUT_CSV_DIR = BASE_DIR / "data" / "output"
REPORTE_FINAL_CSV = OUTPUT_CSV_DIR / "reporte_facturas.csv"

DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_CSV_DIR.mkdir(parents=True, exist_ok=True)

# --- selectores de la página ---
SEL_CUFE = "#DocumentKey"
SEL_NIT = "#SearchDocumentNit"
SEL_BUSCAR = "button.search-document"
SEL_TURNSTILE = ".cf-turnstile[data-sitekey]"
SEL_ERRORES = ".field-validation-error"
SEL_DESCARGA = (
    'a[href*="DownloadPDF"], a:has-text("Descargar PDF"), '
    'button:has-text("Descargar PDF"), a.btn-document'
)
SEL_CAMPOS = (
    "input:not([type='hidden']):not([type='submit'])"
    ":not([type='button']):not([type='checkbox'])"
)
SEL_DIALOGO = ".modal.show, [role='dialog'], .swal2-popup"
RE_BOTON_CONFIRMAR = re.compile(
    r"descargar|aceptar|confirmar|validar|enviar", re.IGNORECASE
)

# devuelve true cuando el widget de cloudflare ya puso el token
JS_TOKEN_LISTO = """() => {
    const el = document.querySelector('[name="cf-turnstile-response"]');
    return !!(el && el.value && el.value.length > 20);
}"""


class ErrorDeConfiguracion(Exception):
    """Errores que no se arreglan reintentando (ej: la DIAN cambió el sitekey)."""


def log(msg):
    print(f"    {msg}", flush=True)


# ------------------------------------------------------------------ #
# Fase 1: bajar los PDFs de la DIAN
# ------------------------------------------------------------------ #
def volcar_estado(page, nombre):
    # guarda captura y html, y lista los elementos para ver qué está pasando
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{nombre}.png"), full_page=True)
        (DEBUG_DIR / f"{nombre}.html").write_text(page.content(), encoding="utf-8")
        elementos = page.evaluate(
            """() => [...document.querySelectorAll('input, button, select, textarea, a.btn, a[href*="Download"]')].map(el => ({
                tag: el.tagName.toLowerCase(), type: el.type || '', id: el.id, name: el.name,
                href: el.getAttribute('href') || '',
                texto: (el.innerText || (el.type === 'hidden' ? '' : el.value) || '').trim().slice(0, 40),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            }))"""
        )
        log(f"[{nombre}] url: {page.url}")
        for el in elementos:
            log(f"[{nombre}]   {el}")
        cuerpo = page.inner_text("body")[:400].replace("\n", " ")
        log(f"[{nombre}] texto: {cuerpo}")
        log(f"[{nombre}] archivos guardados en {DEBUG_DIR}")
    except (PlaywrightError, OSError) as e:
        log(f"no pude guardar el estado '{nombre}': {e}")


def espiar_post(request):
    if request.method == "POST" and "SearchDocument" in request.url:
        try:
            campos = parse_qs(request.post_data or "")
            largos = {k: len(v[0]) for k, v in campos.items()}
            log(f"POST, largo de cada campo: {largos}")
        except Exception:
            log("POST enviado (no pude leer el cuerpo)")


def resolver_turnstile(url):
    if not API_KEY:
        raise RuntimeError("Falta TWOCAPTCHA_API_KEY en tu .env")
    solver = TwoCaptcha(
        API_KEY, defaultTimeout=TIMEOUT_2CAPTCHA_S, pollingInterval=POLLING_2CAPTCHA_S
    )
    res = solver.turnstile(sitekey=TURNSTILE_SITEKEY, url=url)
    token = res.get("code") if isinstance(res, dict) else None
    if not token:
        raise RuntimeError(f"2Captcha no devolvió token: {res}")
    return token


class BancoDeTokens:
    # pide los tokens a 2captcha en hilos aparte para no quedarme esperando
    # cuando llegue el turno de cada CUFE
    def __init__(self):
        self._pool = ThreadPoolExecutor(max_workers=MAX_RESOLUCIONES_PARALELAS)
        self._pedidos = {}
        self._historial = set()

    def pedir(self, clave, url):
        if clave in self._historial:
            return
        self._historial.add(clave)
        self._pedidos[clave] = (self._pool.submit(resolver_turnstile, url), time.time())

    def obtener(self, clave, url):
        pedido = self._pedidos.pop(clave, None)
        if pedido is not None:
            futuro, enviado = pedido
            if time.time() - enviado < TOKEN_TTL_S:
                try:
                    return futuro.result()
                except Exception as e:
                    log(f"el token precargado {clave[1]} falló ({e}), pido otro")
            else:
                log(f"el token precargado {clave[1]} expiró, pido otro")
        return resolver_turnstile(url)

    def cerrar(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


def precargar(banco, lote, desde):
    for cufe, _ in lote[desde : desde + VENTANA_PRECARGA]:
        for etapa, url in URL_POR_ETAPA.items():
            banco.pedir((cufe, etapa), url)


def conseguir_token(page, banco, cufe, etapa, metricas_doc):
    if CAPTCHA_MODE == "manual":
        log("modo manual: marca la casilla de 'Verifique que es un ser humano'...")
        t0 = time.perf_counter()
        page.wait_for_function(JS_TOKEN_LISTO, timeout=TIMEOUT_CAPTCHA_MANUAL_MS)
        metricas_doc["espera_captcha_s"] += time.perf_counter() - t0
        return None
    if banco is None:
        raise RuntimeError("Banco de tokens no inicializado")
    t0 = time.perf_counter()
    token = banco.obtener((cufe, etapa), URL_POR_ETAPA[etapa])
    espera = time.perf_counter() - t0
    metricas_doc["espera_captcha_s"] += espera
    log(f"token de {etapa} listo (esperé {espera:.1f} s)")
    return token


def colocar_token(page, token):
    page.evaluate(
        """(token) => {
            const form = document.querySelector('#search-document-form') || document.querySelector('form') || document.body;
            let campos = [...document.querySelectorAll('[name="cf-turnstile-response"]')];
            if (!campos.length) {
                const c = document.createElement('input');
                c.type = 'hidden'; c.name = 'cf-turnstile-response';
                form.appendChild(c);
                campos = [c];
            }
            campos.forEach(c => { c.value = token; });
        }""",
        token,
    )


def abrir_formulario(page):
    page.goto(URL_DIAN, wait_until="domcontentloaded")
    try:
        page.wait_for_selector(SEL_CUFE, state="visible", timeout=TIMEOUT_FORMULARIO_MS)
        page.wait_for_selector(
            SEL_TURNSTILE, state="attached", timeout=TIMEOUT_FORMULARIO_MS
        )
    except PWTimeout as err:
        volcar_estado(page, "01_sin_formulario")
        raise RuntimeError(
            "No cargó el formulario. Mira _debug/01_sin_formulario.png"
        ) from err

    sitekey = page.get_attribute(SEL_TURNSTILE, "data-sitekey")
    if sitekey != TURNSTILE_SITEKEY:
        raise ErrorDeConfiguracion(f"La DIAN cambió el sitekey a {sitekey}.")


def llenar_formulario(page, cufe, nit):
    page.fill(SEL_CUFE, cufe)
    # el campo del NIT aparece solo después de pegar el CUFE
    page.wait_for_selector(SEL_NIT, state="visible", timeout=TIMEOUT_CAMPO_NIT_MS)
    page.fill(SEL_NIT, nit)
    if page.input_value(SEL_CUFE) != cufe or page.input_value(SEL_NIT) != nit:
        raise RuntimeError("Los campos no quedaron con los valores esperados")


def esperar_resultado(page):
    # revisa cada ratito si salió el botón de descarga, un error o un bloqueo
    fin = time.time() + TIMEOUT_RESULTADO_S
    while time.time() < fin:
        try:
            if page.locator(SEL_DESCARGA).first.is_visible():
                return True, ""
            errores = [
                t.strip()
                for t in page.locator(SEL_ERRORES).all_inner_texts()
                if t.strip()
            ]
            if errores:
                return False, " | ".join(errores)
            if re.search(
                r"bloqueada por controles", page.inner_text("body"), re.IGNORECASE
            ):
                return False, "Bloqueado por controles de seguridad"
        except PlaywrightError:
            pass
        page.wait_for_timeout(INTERVALO_SONDEO_MS)
    return False, "tiempo agotado sin resultado"


def asegurar_captcha_en_resultado(page, banco, cufe, metricas_doc):
    if page.locator(SEL_TURNSTILE).count() == 0:
        return
    token = conseguir_token(page, banco, cufe, "descarga", metricas_doc)
    if token:
        colocar_token(page, token)


def reingresar_nit_si_lo_piden(page, nit):
    # a veces sale un modal pidiendo el NIT otra vez antes de descargar
    dialogo = page.locator(SEL_DIALOGO).first
    try:
        dialogo.wait_for(state="visible", timeout=TIMEOUT_DIALOGO_MS)
    except PWTimeout:
        return
    campo = dialogo.locator(SEL_CAMPOS).first
    if campo.count():
        campo.fill(nit)
    dialogo.get_by_role("button", name=RE_BOTON_CONFIRMAR).first.click()


def descargar_pdf(page, banco, cufe, nit, metricas_doc):
    destino = DOWNLOADS_DIR / f"{cufe}.pdf"
    asegurar_captcha_en_resultado(page, banco, cufe, metricas_doc)
    with page.expect_download(timeout=TIMEOUT_DESCARGA_MS) as info:
        page.locator(SEL_DESCARGA).first.click(force=True)
        reingresar_nit_si_lo_piden(page, nit)
    info.value.save_as(str(destino))
    return destino


def procesar_cufe(page, banco, cufe, nit, volcar_resultado):
    prefijo = cufe[:8]
    ultimo_error = ""
    metricas_doc = {"intentos": 0, "espera_captcha_s": 0.0, "tiempo_total_s": 0.0}
    t_inicio_doc = time.perf_counter()

    for intento in range(1, MAX_INTENTOS + 1):
        metricas_doc["intentos"] = intento
        t_intento = time.perf_counter()
        log(f"intento {intento} de {MAX_INTENTOS}")
        try:
            abrir_formulario(page)
            llenar_formulario(page, cufe, nit)
            token = conseguir_token(page, banco, cufe, "busqueda", metricas_doc)
            if token:
                colocar_token(page, token)
            page.click(SEL_BUSCAR)

            ok, detalle = esperar_resultado(page)
            if not ok:
                ultimo_error = detalle
                log(
                    f"sin resultado ({time.perf_counter() - t_intento:.1f} s): {detalle}"
                )
                volcar_estado(page, f"02_{prefijo}_intento{intento}")
                continue

            if volcar_resultado:
                volcar_estado(page, "03_pagina_de_resultado")

            ruta_pdf = descargar_pdf(page, banco, cufe, nit, metricas_doc)
            metricas_doc["tiempo_total_s"] = time.perf_counter() - t_inicio_doc
            return "OK", str(ruta_pdf), metricas_doc

        except ErrorDeConfiguracion:
            raise
        except (PlaywrightError, RuntimeError) as e:
            ultimo_error = str(e).splitlines()[0]
            log(
                f"error en el intento {intento} ({time.perf_counter() - t_intento:.1f} s): {ultimo_error}"
            )
            volcar_estado(page, f"04_{prefijo}_error{intento}")

    metricas_doc["tiempo_total_s"] = time.perf_counter() - t_inicio_doc
    return "FALLO", ultimo_error, metricas_doc


def leer_cufes():
    if not CSV_ENTRADA.exists():
        raise SystemExit(f"Crea {CSV_ENTRADA.name} con columnas: cufe,nit")
    filas = []
    with CSV_ENTRADA.open(newline="", encoding="utf-8-sig") as f:
        for fila in csv.DictReader(f):
            cufe = (fila.get("cufe") or "").strip()
            if cufe:
                filas.append((cufe, (fila.get("nit") or NIT_DEFECTO).strip()))
    return filas


def lanzar_navegador(p):
    args = ["--disable-blink-features=AutomationControlled"]
    try:
        return p.chromium.launch(channel="chrome", headless=False, args=args)
    except PlaywrightError:
        log("no encontré Chrome, uso el Chromium de Playwright")
        return p.chromium.launch(headless=False, args=args)


# ------------------------------------------------------------------ #
# Fase 2: leer los PDFs y armar el CSV
# ------------------------------------------------------------------ #
def limpiar(celda):
    return str(celda or "").replace("\n", " ").strip()


def encontrar_indices_columnas(tabla):
    # el header de la tabla de productos casi siempre está en las primeras filas
    for idx_fila, fila in enumerate(tabla[:3]):
        fila_texto = [
            re.sub(r"\s+", " ", str(celda or "")).strip().lower() for celda in fila
        ]

        if "código" in fila_texto or "codigo" in fila_texto:
            indices = {}
            for col_idx, texto in enumerate(fila_texto):
                if "código" in texto or "codigo" in texto:
                    indices["codigo"] = col_idx
                elif "descripción" in texto or "descripcion" in texto:
                    indices["descripcion"] = col_idx
                elif "cantidad" in texto:
                    indices["cantidad"] = col_idx
                elif "precio unitario" in texto and "venta" not in texto:
                    indices["precio"] = col_idx

            if len(indices) == 4:
                return idx_fila, indices
    return None, None


def abrir_pdf(ruta_pdf):
    # prueba con la contraseña del .env y si falla (PDF sin cifrar) lo abre normal
    try:
        return pdfplumber.open(ruta_pdf, password=PASSWORD or None)
    except Exception:
        return pdfplumber.open(ruta_pdf, password=None)


def extraer_datos_pdf(ruta_pdf):
    datos = {
        "numero_factura": "NO_ENCONTRADO",
        "fecha_emision": "NO_ENCONTRADO",
        "nit_emisor": "NO_ENCONTRADO",
        "detalles_producto": [],
    }

    try:
        with abrir_pdf(ruta_pdf) as pdf:
            texto_completo = ""
            for page in pdf.pages:
                texto_completo += (page.extract_text() or "") + "\n"

            num_factura = re.search(
                r"Número de Factura:\s*([A-Z0-9-]+)", texto_completo
            )
            fecha_emision = re.search(r"Fecha de Emisión:\s*([\d/]+)", texto_completo)
            nit_emisor = re.search(r"Nit del Emisor:\s*(\d+)", texto_completo)

            if num_factura:
                datos["numero_factura"] = num_factura.group(1).strip()
            if fecha_emision:
                datos["fecha_emision"] = fecha_emision.group(1).strip()
            if nit_emisor:
                datos["nit_emisor"] = nit_emisor.group(1).strip()

            for page in pdf.pages:
                for tabla in page.extract_tables():
                    if not tabla:
                        continue

                    fila_header_idx, idx_cols = encontrar_indices_columnas(tabla)
                    if fila_header_idx is None or idx_cols is None:
                        continue

                    for fila in tabla[fila_header_idx + 1 :]:
                        codigo = limpiar(fila[idx_cols["codigo"]])
                        descripcion = re.sub(
                            r"\s+", " ", limpiar(fila[idx_cols["descripcion"]])
                        )
                        cantidad = limpiar(fila[idx_cols["cantidad"]])
                        precio = (
                            limpiar(fila[idx_cols["precio"]]).replace("$", "").strip()
                        )

                        # si la cantidad no es un número es una fila de totales u otra cosa
                        if re.match(r"^\d+[\.,]?\d*$", cantidad) and descripcion:
                            datos["detalles_producto"].append(
                                {
                                    "Codigo": codigo,
                                    "Descripcion": descripcion,
                                    "Cantidad": cantidad,
                                    "Precio_Unitario": precio,
                                }
                            )

    except Exception as e:
        log(f"error leyendo {ruta_pdf.name}: {e}")

    return datos


def ejecutar_fase_extraccion():
    print("\nFASE 2: leyendo los PDFs")

    t_inicio_fase2 = time.perf_counter()
    archivos_pdf = sorted(DOWNLOADS_DIR.glob("*.pdf"))

    if not archivos_pdf:
        log(f"no hay PDFs en {DOWNLOADS_DIR}")
        return 0.0, [], 0, 0

    todas_las_facturas = []
    tiempos_pdf = []
    pdfs_sin_items = 0

    for idx, archivo in enumerate(archivos_pdf, start=1):
        t0_pdf = time.perf_counter()
        datos_factura = extraer_datos_pdf(archivo)
        duracion_pdf = time.perf_counter() - t0_pdf
        tiempos_pdf.append(duracion_pdf)

        cufe_ending = archivo.stem[-4:]
        items = datos_factura["detalles_producto"]

        if not items:
            pdfs_sin_items += 1

        for prod in items:
            todas_las_facturas.append(
                {
                    "CUFE_ending": cufe_ending,
                    "Numero_Factura": datos_factura["numero_factura"],
                    "Fecha_Emision": datos_factura["fecha_emision"],
                    "NIT_Emisor": datos_factura["nit_emisor"],
                    "Codigo": prod["Codigo"],
                    "Descripcion": prod["Descripcion"],
                    "Cantidad": prod["Cantidad"],
                    "Precio_Unitario": prod["Precio_Unitario"],
                }
            )

        print(
            f"({idx}/{len(archivos_pdf)}) ...{cufe_ending} "
            f"factura {datos_factura['numero_factura']}, "
            f"{len(items)} ítems, {duracion_pdf:.3f} s"
        )

    t0_csv = time.perf_counter()
    if todas_las_facturas:
        df = pd.DataFrame(todas_las_facturas)
        df.to_csv(REPORTE_FINAL_CSV, index=False, sep=",", encoding="utf-8-sig")
        t_csv = time.perf_counter() - t0_csv
        print(
            f"\nCSV guardado con {len(df)} filas ({t_csv:.3f} s): {REPORTE_FINAL_CSV}"
        )
    else:
        print("\nNo se sacó ningún producto de los PDFs, no se generó el CSV")

    t_total_fase2 = time.perf_counter() - t_inicio_fase2
    return t_total_fase2, tiempos_pdf, len(todas_las_facturas), pdfs_sin_items


# ------------------------------------------------------------------ #
# Resumen final
# ------------------------------------------------------------------ #
def imprimir_resumen(
    t_fase1,
    t_fase2,
    t_total,
    resultados_rpa,
    t_pausas_total,
    tiempos_pdf,
    total_items,
    pdfs_sin_items,
):
    # los "YA_EXISTE" no pasaron por el navegador, así que no entran en los tiempos
    procesados = [r for r in resultados_rpa if r["estado"] in ("OK", "FALLO")]
    tiempos_descarga = [r["metricas"]["tiempo_total_s"] for r in procesados]
    esperas_captcha = [r["metricas"]["espera_captcha_s"] for r in procesados]
    intentos_totales = sum(r["metricas"]["intentos"] for r in procesados)
    primer_intento = sum(
        1 for r in procesados if r["estado"] == "OK" and r["metricas"]["intentos"] == 1
    )

    ok_rpa = sum(1 for r in resultados_rpa if r["estado"] == "OK")
    ya_existian = sum(1 for r in resultados_rpa if r["estado"] == "YA_EXISTE")
    fallos_rpa = sum(1 for r in resultados_rpa if r["estado"] == "FALLO")

    print("\n" + "-" * 60)
    print("RESUMEN")

    print("\nTiempos")
    print(f"  fase 1 (descarga):   {t_fase1:8.2f} s  ({t_fase1 / t_total * 100:.1f}%)")
    print(f"  fase 2 (PDFs y CSV): {t_fase2:8.2f} s  ({t_fase2 / t_total * 100:.1f}%)")
    print(f"  total:               {t_total:8.2f} s")

    print("\nFase 1")
    print(
        f"  {len(resultados_rpa)} CUFEs: {ok_rpa} descargados, "
        f"{ya_existian} ya estaban, {fallos_rpa} fallaron"
    )
    if tiempos_descarga:
        n = len(tiempos_descarga)
        desv = statistics.stdev(tiempos_descarga) if n > 1 else 0.0
        t_captcha = sum(esperas_captcha)
        print(
            f"  tiempo por CUFE: promedio {statistics.mean(tiempos_descarga):.2f} s, "
            f"mín {min(tiempos_descarga):.2f} s, máx {max(tiempos_descarga):.2f} s, "
            f"desv. {desv:.2f} s"
        )
        print(
            f"  a la primera (sin reintentos): {primer_intento} de {n} "
            f"({primer_intento / n * 100:.1f}%)"
        )
        print(
            f"  intentos en total: {intentos_totales} "
            f"({intentos_totales / n:.2f} por CUFE)"
        )
        print(
            f"  esperando a 2captcha: {t_captcha:.2f} s "
            f"({t_captcha / t_fase1 * 100:.1f}% de la fase 1)"
        )
        print(
            f"  pausas entre CUFEs: {t_pausas_total:.2f} s "
            f"({t_pausas_total / t_fase1 * 100:.1f}% de la fase 1)"
        )

    print("\nFase 2")
    if tiempos_pdf:
        velocidad = len(tiempos_pdf) / t_fase2 if t_fase2 > 0 else 0
        print(f"  {len(tiempos_pdf)} PDFs leídos, {total_items} líneas de producto")
        print(
            f"  tiempo por PDF: promedio {statistics.mean(tiempos_pdf):.3f} s, "
            f"mín {min(tiempos_pdf):.3f} s, máx {max(tiempos_pdf):.3f} s"
        )
        print(f"  velocidad: {velocidad:.2f} PDFs por segundo")
        print(f"  PDFs sin ningún ítem: {pdfs_sin_items}")

    print("\nConfiguración usada")
    print(f"  timeout de descarga: {TIMEOUT_DESCARGA_MS / 1000:.0f} s por intento")
    print(f"  timeout del diálogo del NIT: {TIMEOUT_DIALOGO_MS / 1000:.0f} s")
    print(f"  timeout del resultado de la búsqueda: {TIMEOUT_RESULTADO_S} s")
    print(
        f"  2captcha: {MAX_RESOLUCIONES_PARALELAS} hilos, precarga de "
        f"{VENTANA_PRECARGA} docs, el token dura {TOKEN_TTL_S} s"
    )
    print(
        f"  pausa entre docs: entre {PAUSA_ENTRE_DOCS_S[0]} y {PAUSA_ENTRE_DOCS_S[1]} s "
        f"(aleatoria)"
    )
    print(f"  reintentos: máximo {MAX_INTENTOS} por CUFE")
    print()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--uno", action="store_true", help="procesar solo el primer CUFE"
    )
    args = parser.parse_args()

    t_inicio_global = time.perf_counter()

    # ---- fase 1: descarga ----
    print("FASE 1: descargando facturas de la DIAN")

    t_inicio_fase1 = time.perf_counter()
    cufes = leer_cufes()
    if args.uno:
        cufes = cufes[:1]

    resultados_rpa = []
    por_hacer = []
    for cufe, nit in cufes:
        if (DOWNLOADS_DIR / f"{cufe}.pdf").exists():
            resultados_rpa.append(
                {
                    "cufe": cufe,
                    "estado": "YA_EXISTE",
                    "metricas": {
                        "intentos": 0,
                        "espera_captcha_s": 0.0,
                        "tiempo_total_s": 0.0,
                    },
                }
            )
        else:
            por_hacer.append((cufe, nit))

    print(
        f"{len(cufes)} CUFEs en el csv, {len(resultados_rpa)} ya estaban descargados, "
        f"faltan {len(por_hacer)} (captcha: {CAPTCHA_MODE})"
    )

    banco = BancoDeTokens() if CAPTCHA_MODE == "2captcha" else None
    t_pausas_total = 0.0

    try:
        if por_hacer:
            with sync_playwright() as p:
                browser = lanzar_navegador(p)
                context = browser.new_context(
                    accept_downloads=True, locale="es-CO", timezone_id="America/Bogota"
                )
                page = context.new_page()
                if DEPURAR_POST:
                    page.on("request", espiar_post)
                try:
                    for i, (cufe, nit) in enumerate(por_hacer, start=1):
                        print(f"\n({i}/{len(por_hacer)}) CUFE {cufe[:12]}... NIT {nit}")
                        if banco is not None:
                            precargar(banco, por_hacer, i - 1)

                        estado, detalle, metricas_doc = procesar_cufe(
                            page,
                            banco,
                            cufe,
                            nit,
                            volcar_resultado=(i == 1 and VOLCAR_PRIMER_RESULTADO),
                        )
                        log(
                            f"{estado} en {metricas_doc['tiempo_total_s']:.1f} s "
                            f"(captcha {metricas_doc['espera_captcha_s']:.1f} s, "
                            f"intentos: {metricas_doc['intentos']})"
                        )
                        if estado == "FALLO":
                            log(f"último error: {detalle}")
                        resultados_rpa.append(
                            {"cufe": cufe, "estado": estado, "metricas": metricas_doc}
                        )

                        if i < len(por_hacer):
                            pausa = random.uniform(*PAUSA_ENTRE_DOCS_S)
                            t_pausas_total += pausa
                            time.sleep(pausa)
                finally:
                    browser.close()
    finally:
        if banco is not None:
            banco.cerrar()

    t_total_fase1 = time.perf_counter() - t_inicio_fase1

    # ---- fase 2: leer PDFs ----
    t_total_fase2, tiempos_pdf, total_items, pdfs_sin_items = ejecutar_fase_extraccion()

    t_total_global = time.perf_counter() - t_inicio_global
    imprimir_resumen(
        t_total_fase1,
        t_total_fase2,
        t_total_global,
        resultados_rpa,
        t_pausas_total,
        tiempos_pdf,
        total_items,
        pdfs_sin_items,
    )


if __name__ == "__main__":
    main()
