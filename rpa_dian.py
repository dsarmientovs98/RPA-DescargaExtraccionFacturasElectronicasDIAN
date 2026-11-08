import argparse
import csv
import os
import random
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import parse_qs

from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright
from twocaptcha import TwoCaptcha

load_dotenv()
CAPTCHA_MODE = os.getenv("CAPTCHA_MODE", "2captcha").strip().lower()
API_KEY = os.getenv("TWOCAPTCHA_API_KEY")
if CAPTCHA_MODE == "2captcha" and not API_KEY:
    raise ValueError("Falta TWOCAPTCHA_API_KEY en tu .env (o usa CAPTCHA_MODE=manual)")

# =========================================================================== #
# CONSTANTES  (ajusta aquí; no hay números "sueltos" en la lógica)
# =========================================================================== #
# --- Sitio ---
NIT_DEFECTO = "900156264"
URL_DIAN = "https://catalogo-vpfe.dian.gov.co/User/SearchDocument"
URL_RESULTADO = "https://catalogo-vpfe.dian.gov.co/Document/ShowDocumentToPublic"
TURNSTILE_SITEKEY = "0x4AAAAAAAg1WuNb-OnOa76z"  # si la DIAN lo cambia, el script avisa

# --- Esperas de Playwright (milisegundos) ---
TIMEOUT_FORMULARIO_MS = 20_000
TIMEOUT_CAMPO_NIT_MS = 5_000
TIMEOUT_DIALOGO_MS = 10_000  # Ampliado para dar margen al modal gubernamental
TIMEOUT_DESCARGA_MS = 30_000  # Reducido (Fail-fast): si en 30s no descarga, se aborta
TIMEOUT_CAPTCHA_MANUAL_MS = 120_000
INTERVALO_SONDEO_MS = 200

# --- Esperas propias (segundos) ---
TIMEOUT_RESULTADO_S = 30
PAUSA_ENTRE_DOCS_S = (1.5, 3.0)  # (mínimo, máximo)

# --- 2Captcha ---
TIMEOUT_2CAPTCHA_S = 120
POLLING_2CAPTCHA_S = 5
TOKEN_TTL_S = 240
VENTANA_PRECARGA = 4
MAX_RESOLUCIONES_PARALELAS = 8

# --- Reintentos y depuración ---
MAX_INTENTOS = 3
VOLCAR_PRIMER_RESULTADO = False
DEPURAR_POST = False
LARGO_MIN_TOKEN = 20
LARGO_PREFIJO_ARCHIVO = 8
LARGO_PREFIJO_LOG = 12
LARGO_TEXTO_CUERPO = 400
LARGO_TEXTO_ELEMENTO = 40

# --- Etapas (clave de los tokens precargados) ---
ETAPA_BUSQUEDA = "busqueda"
ETAPA_DESCARGA = "descarga"
URL_POR_ETAPA = {ETAPA_BUSQUEDA: URL_DIAN, ETAPA_DESCARGA: URL_RESULTADO}

# --- Rutas ---
BASE_DIR = Path(__file__).parent
CSV_ENTRADA = BASE_DIR / "cufes.csv"
OUTPUT_DIR = BASE_DIR / "data" / "downloads"
DEBUG_DIR = OUTPUT_DIR / "_debug"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)

# --- Selectores ---
SEL_CUFE = "#DocumentKey"
SEL_NIT = "#SearchDocumentNit"
SEL_BUSCAR = "button.search-document"
SEL_TURNSTILE = ".cf-turnstile[data-sitekey]"
SEL_ERRORES = ".field-validation-error"
SEL_DESCARGA = (
    'a[href*="DownloadPDF"], a:has-text("Descargar PDF"), '
    'button:has-text("Descargar PDF"), a.btn-document'
)
SEL_CAMPOS = "input:not([type='hidden']):not([type='submit']):not([type='button']):not([type='checkbox'])"
SEL_DIALOGO = ".modal.show, [role='dialog'], .swal2-popup"
RE_BOTON_CONFIRMAR = re.compile(
    r"descargar|aceptar|confirmar|validar|enviar", re.IGNORECASE
)

JS_TOKEN_LISTO = (
    "() => { const el = document.querySelector('[name=\"cf-turnstile-response\"]');"
    f" return !!(el && el.value && el.value.length > {LARGO_MIN_TOKEN}); }}"
)


class ErrorDeConfiguracion(Exception):
    """Error que no se arregla reintentando."""


def log(msg):
    print(f"   -> {msg}", flush=True)


# --------------------------------------------------------------------------- #
# Diagnóstico
# --------------------------------------------------------------------------- #
def volcar_estado(page, nombre):
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{nombre}.png"), full_page=True)
        (DEBUG_DIR / f"{nombre}.html").write_text(page.content(), encoding="utf-8")
        filas = page.evaluate(
            """(largo) => [...document.querySelectorAll('input, button, select, textarea, a.btn, a[href*="Download"]')].map(el => ({
                tag: el.tagName.toLowerCase(), type: el.type || '', id: el.id, name: el.name,
                href: el.getAttribute('href') || '',
                texto: (el.innerText || (el.type === 'hidden' ? '' : el.value) || '').trim().slice(0, largo),
                visible: !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length)
            }))""",
            LARGO_TEXTO_ELEMENTO,
        )
        log(f"[{nombre}] URL = {page.url}")
        for fila in filas:
            log(f"[{nombre}]   {fila}")
        cuerpo = page.inner_text("body")[:LARGO_TEXTO_CUERPO].replace("\n", " ")
        log(f"[{nombre}] texto visible: {cuerpo}")
        log(f"[{nombre}] archivos en {DEBUG_DIR}")
    except (PlaywrightError, OSError) as e:
        log(f"(no pude volcar estado '{nombre}': {e})")


def espiar_post(request):
    if request.method == "POST" and "SearchDocument" in request.url:
        try:
            campos = parse_qs(request.post_data or "")
            log(
                f"POST -> longitud de cada campo: { {k: len(v[0]) for k, v in campos.items()} }"
            )
        except Exception:  # noqa: BLE001
            log("POST enviado (no pude leer el cuerpo)")


# --------------------------------------------------------------------------- #
# Tokens de Turnstile
# --------------------------------------------------------------------------- #
def resolver_turnstile(url):
    api_key = API_KEY
    if not api_key:
        raise RuntimeError("Falta TWOCAPTCHA_API_KEY en tu .env")
    solver = TwoCaptcha(
        api_key, defaultTimeout=TIMEOUT_2CAPTCHA_S, pollingInterval=POLLING_2CAPTCHA_S
    )
    res = solver.turnstile(sitekey=TURNSTILE_SITEKEY, url=url)
    token = res.get("code") if isinstance(res, dict) else None
    if not token:
        raise RuntimeError(f"2Captcha no devolvió token: {res}")
    return token


class BancoDeTokens:
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
                except Exception as e:  # noqa: BLE001
                    log(f"El token precargado {clave} falló ({e}); pido otro.")
            else:
                log(f"El token precargado {clave} ya expiró; pido otro.")
        return resolver_turnstile(url)

    def cerrar(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


def precargar(banco, lote, desde):
    for cufe, _ in lote[desde : desde + VENTANA_PRECARGA]:
        for etapa, url in URL_POR_ETAPA.items():
            banco.pedir((cufe, etapa), url)


def conseguir_token(page, banco, cufe, etapa):
    if CAPTCHA_MODE == "manual":
        log("MODO MANUAL: marca la casilla 'Verifique que es un ser humano'...")
        page.wait_for_function(JS_TOKEN_LISTO, timeout=TIMEOUT_CAPTCHA_MANUAL_MS)
        return None
    if banco is None:
        raise RuntimeError("Banco de tokens no inicializado")
    t0 = time.perf_counter()
    token = banco.obtener((cufe, etapa), URL_POR_ETAPA[etapa])
    log(f"Token '{etapa}' listo (esperé {time.perf_counter() - t0:.1f} s)")
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


# --------------------------------------------------------------------------- #
# Pasos del flujo
# --------------------------------------------------------------------------- #
def abrir_formulario(page):
    page.goto(URL_DIAN, wait_until="domcontentloaded")
    try:
        page.wait_for_selector(SEL_CUFE, state="visible", timeout=TIMEOUT_FORMULARIO_MS)
        page.wait_for_selector(
            SEL_TURNSTILE, state="attached", timeout=TIMEOUT_FORMULARIO_MS
        )
    except PWTimeout:
        volcar_estado(page, "01_sin_formulario")
        raise RuntimeError("No cargó el formulario. Mira _debug/01_sin_formulario.png")

    sitekey = page.get_attribute(SEL_TURNSTILE, "data-sitekey")
    if sitekey != TURNSTILE_SITEKEY:
        raise ErrorDeConfiguracion(f"La DIAN cambió el sitekey a {sitekey}.")


def llenar_formulario(page, cufe, nit):
    page.fill(SEL_CUFE, cufe)
    page.wait_for_selector(SEL_NIT, state="visible", timeout=TIMEOUT_CAMPO_NIT_MS)
    page.fill(SEL_NIT, nit)
    if page.input_value(SEL_CUFE) != cufe or page.input_value(SEL_NIT) != nit:
        raise RuntimeError("Los campos no quedaron con los valores esperados")


def esperar_resultado(page):
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


def asegurar_captcha_en_resultado(page, banco, cufe):
    if page.locator(SEL_TURNSTILE).count() == 0:
        return
    token = conseguir_token(page, banco, cufe, ETAPA_DESCARGA)
    if token:
        colocar_token(page, token)


def reingresar_nit_si_lo_piden(page, nit):
    dialogo = page.locator(SEL_DIALOGO).first
    try:
        dialogo.wait_for(state="visible", timeout=TIMEOUT_DIALOGO_MS)
    except PWTimeout:
        return
    campo = dialogo.locator(SEL_CAMPOS).first
    if campo.count():
        campo.fill(nit)
    dialogo.get_by_role("button", name=RE_BOTON_CONFIRMAR).first.click()


def descargar_pdf(page, banco, cufe, nit):
    destino = OUTPUT_DIR / f"{cufe}.pdf"
    asegurar_captcha_en_resultado(page, banco, cufe)
    with page.expect_download(timeout=TIMEOUT_DESCARGA_MS) as info:
        page.locator(SEL_DESCARGA).first.click(force=True)
        reingresar_nit_si_lo_piden(page, nit)
    info.value.save_as(str(destino))
    return destino


# --------------------------------------------------------------------------- #
# Orquestación
# --------------------------------------------------------------------------- #
def procesar_cufe(page, banco, cufe, nit, volcar_resultado):
    prefijo = cufe[:LARGO_PREFIJO_ARCHIVO]
    ultimo_error = ""
    for intento in range(1, MAX_INTENTOS + 1):
        log(f"Intento {intento}/{MAX_INTENTOS}")
        try:
            abrir_formulario(page)
            llenar_formulario(page, cufe, nit)
            token = conseguir_token(page, banco, cufe, ETAPA_BUSQUEDA)
            if token:
                colocar_token(page, token)
            page.click(SEL_BUSCAR)

            ok, detalle = esperar_resultado(page)
            if not ok:
                ultimo_error = detalle
                log(f"Sin resultado: {detalle}")
                volcar_estado(page, f"02_{prefijo}_intento{intento}")
                continue

            if volcar_resultado:
                volcar_estado(page, "03_pagina_de_resultado")
            return "OK", str(descargar_pdf(page, banco, cufe, nit))

        except ErrorDeConfiguracion:
            raise
        except (PlaywrightError, RuntimeError) as e:
            ultimo_error = str(e).splitlines()[0]
            log(f"Error: {ultimo_error}")
            volcar_estado(page, f"04_{prefijo}_error{intento}")

    return "FALLO", ultimo_error


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
        log("Chrome no está instalado; uso el Chromium de Playwright.")
        return p.chromium.launch(headless=False, args=args)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--uno", action="store_true", help="procesar solo el primer CUFE")
    args = ap.parse_args()

    cufes = leer_cufes()
    if args.uno:
        cufes = cufes[:1]

    resultados = []
    por_hacer = []
    for cufe, nit in cufes:
        existente = OUTPUT_DIR / f"{cufe}.pdf"
        if existente.exists():
            resultados.append({"estado": "YA_EXISTE"})
        else:
            por_hacer.append((cufe, nit))

    print(
        f"\n--- {len(por_hacer)} CUFE(s) por descargar ({len(resultados)} ya existían) | modo CAPTCHA: {CAPTCHA_MODE} ---"
    )
    inicio = time.perf_counter()
    banco = BancoDeTokens() if CAPTCHA_MODE == "2captcha" else None

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
                        print(
                            f"\n[{i}/{len(por_hacer)}] CUFE {cufe[:LARGO_PREFIJO_LOG]}... | NIT {nit}"
                        )
                        if banco is not None:
                            precargar(banco, por_hacer, i - 1)
                        t0 = time.perf_counter()
                        estado, detalle = procesar_cufe(
                            page,
                            banco,
                            cufe,
                            nit,
                            volcar_resultado=(i == 1 and VOLCAR_PRIMER_RESULTADO),
                        )
                        log(f"{estado} en {time.perf_counter() - t0:.1f} s")
                        resultados.append({"estado": estado})

                        if i < len(por_hacer):
                            time.sleep(random.uniform(*PAUSA_ENTRE_DOCS_S))
                finally:
                    browser.close()
    finally:
        if banco is not None:
            banco.cerrar()

    ok = sum(1 for r in resultados if r["estado"] in ("OK", "YA_EXISTE"))
    print(
        f"\n=== Listo: {ok}/{len(resultados)} procesados en {time.perf_counter() - inicio:.1f} s ==="
    )


if __name__ == "__main__":
    main()
