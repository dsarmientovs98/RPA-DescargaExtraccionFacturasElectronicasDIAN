import csv
import os
import random
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd
import pdfplumber
from dotenv import load_dotenv
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PWTimeout
from playwright.sync_api import sync_playwright
from twocaptcha import TwoCaptcha

load_dotenv()
API_KEY = os.getenv("TWOCAPTCHA_API_KEY")
PASSWORD = os.getenv("PDF_PASSWORD")

if not API_KEY:
    raise ValueError("Falta TWOCAPTCHA_API_KEY en el .env")

# sitio de la DIAN
NIT_DEFECTO = "900156264"
URL_DIAN = "https://catalogo-vpfe.dian.gov.co/User/SearchDocument"
URL_RESULTADO = "https://catalogo-vpfe.dian.gov.co/Document/ShowDocumentToPublic"
TURNSTILE_SITEKEY = "0x4AAAAAAAg1WuNb-OnOa76z"

# esperas de playwright (ms)
TIMEOUT_FORMULARIO_MS = 20_000
TIMEOUT_CAMPO_NIT_MS = 5_000
TIMEOUT_DIALOGO_MS = 10_000
TIMEOUT_DESCARGA_MS = 30_000
INTERVALO_SONDEO_MS = 200

# esperas propias (s)
TIMEOUT_RESULTADO_S = 30
PAUSA_ENTRE_DOCS_S = (1.5, 3.0)

# 2captcha
TIMEOUT_2CAPTCHA_S = 120
POLLING_2CAPTCHA_S = 5
TOKEN_TTL_S = 240
VENTANA_PRECARGA = 4
MAX_RESOLUCIONES_PARALELAS = 8

MAX_INTENTOS = 3

# cada CUFE necesita un token en la búsqueda y otro en la descarga
URLS_ETAPA = {"busqueda": URL_DIAN, "descarga": URL_RESULTADO}

# rutas
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

# selectores
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


# esta no se arregla reintentando
class ErrorDeConfiguracion(Exception):
    pass


# ---------------------------------------------------------------- descarga ---


def guardar_debug(page, nombre):
    try:
        page.screenshot(path=str(DEBUG_DIR / f"{nombre}.png"), full_page=True)
        (DEBUG_DIR / f"{nombre}.html").write_text(page.content(), encoding="utf-8")
        texto = page.inner_text("body")[:400].replace("\n", " ")
        print(f"   guardé captura y html en _debug/{nombre}")
        print(f"   la página decía: {texto}")
    except (PlaywrightError, OSError) as e:
        print(f"   no pude guardar el debug '{nombre}': {e}")


def resolver_turnstile(url):
    solver = TwoCaptcha(
        API_KEY, defaultTimeout=TIMEOUT_2CAPTCHA_S, pollingInterval=POLLING_2CAPTCHA_S
    )
    res = solver.turnstile(sitekey=TURNSTILE_SITEKEY, url=url)
    token = res.get("code") if isinstance(res, dict) else None
    if not token:
        raise RuntimeError(f"2Captcha no devolvió token: {res}")
    return token


class BancoDeTokens:
    # pide los tokens por adelantado en hilos para no esperar en cada factura
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
                    print(
                        f"   el token precargado de {clave[1]} falló ({e}), pido otro"
                    )
            else:
                print(f"   el token precargado de {clave[1]} ya venció, pido otro")
        return resolver_turnstile(url)

    def cerrar(self):
        self._pool.shutdown(wait=False, cancel_futures=True)


def precargar(banco, lote, desde):
    for cufe, _ in lote[desde : desde + VENTANA_PRECARGA]:
        for etapa, url in URLS_ETAPA.items():
            banco.pedir((cufe, etapa), url)


def conseguir_token(banco, cufe, etapa, metricas):
    t0 = time.perf_counter()
    token = banco.obtener((cufe, etapa), URLS_ETAPA[etapa])
    espera = time.perf_counter() - t0
    metricas["espera_captcha_s"] += espera
    print(f"   token de {etapa} listo (esperé {espera:.1f} s)")
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
        guardar_debug(page, "01_sin_formulario")
        raise RuntimeError(
            "No cargó el formulario. Mira _debug/01_sin_formulario.png"
        ) from err

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


def captcha_en_resultado(page, banco, cufe, metricas):
    # la página de resultado a veces trae su propio turnstile
    if page.locator(SEL_TURNSTILE).count() == 0:
        return
    token = conseguir_token(banco, cufe, "descarga", metricas)
    colocar_token(page, token)


def reingresar_nit(page, nit):
    # al darle descargar a veces sale un modal pidiendo el NIT otra vez
    dialogo = page.locator(SEL_DIALOGO).first
    try:
        dialogo.wait_for(state="visible", timeout=TIMEOUT_DIALOGO_MS)
    except PWTimeout:
        return
    campo = dialogo.locator(SEL_CAMPOS).first
    if campo.count():
        campo.fill(nit)
    dialogo.get_by_role("button", name=RE_BOTON_CONFIRMAR).first.click()


def descargar_pdf(page, banco, cufe, nit, metricas):
    destino = DOWNLOADS_DIR / f"{cufe}.pdf"
    captcha_en_resultado(page, banco, cufe, metricas)
    with page.expect_download(timeout=TIMEOUT_DESCARGA_MS) as info:
        page.locator(SEL_DESCARGA).first.click(force=True)
        reingresar_nit(page, nit)
    info.value.save_as(str(destino))
    return destino


def procesar_cufe(page, banco, cufe, nit):
    prefijo = cufe[:8]
    ultimo_error = ""
    metricas = {"intentos": 0, "espera_captcha_s": 0.0, "tiempo_total_s": 0.0}
    t_inicio = time.perf_counter()

    for intento in range(1, MAX_INTENTOS + 1):
        metricas["intentos"] = intento
        t_intento = time.perf_counter()
        print(f"   intento {intento}/{MAX_INTENTOS}")
        try:
            abrir_formulario(page)
            llenar_formulario(page, cufe, nit)
            token = conseguir_token(banco, cufe, "busqueda", metricas)
            colocar_token(page, token)
            page.click(SEL_BUSCAR)

            ok, detalle = esperar_resultado(page)
            if not ok:
                ultimo_error = detalle
                seg = time.perf_counter() - t_intento
                print(f"   no apareció el resultado ({seg:.1f} s): {detalle}")
                guardar_debug(page, f"02_{prefijo}_intento{intento}")
                continue

            ruta_pdf = descargar_pdf(page, banco, cufe, nit, metricas)
            metricas["tiempo_total_s"] = time.perf_counter() - t_inicio
            return "OK", str(ruta_pdf), metricas

        except ErrorDeConfiguracion:
            raise
        except (PlaywrightError, RuntimeError) as e:
            ultimo_error = str(e).splitlines()[0]
            seg = time.perf_counter() - t_intento
            print(f"   falló el intento {intento} ({seg:.1f} s): {ultimo_error}")
            guardar_debug(page, f"04_{prefijo}_error{intento}")

    metricas["tiempo_total_s"] = time.perf_counter() - t_inicio
    return "FALLO", ultimo_error, metricas


def leer_cufes():
    if not CSV_ENTRADA.exists():
        raise SystemExit(f"Crea {CSV_ENTRADA.name} con las columnas: cufe,nit")
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
        print("Chrome no está instalado, uso el chromium de playwright")
        return p.chromium.launch(headless=False, args=args)


# --------------------------------------------------------------- leer pdfs ---


def encontrar_indices_columnas(tabla):
    # el encabezado de la tabla de productos está en las primeras filas
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
    # primero con la clave, si el pdf no está cifrado lo abre sin ella
    try:
        return pdfplumber.open(ruta_pdf, password=PASSWORD or None)
    except Exception:
        return pdfplumber.open(ruta_pdf, password=None)


def limpiar(celda):
    return str(celda or "").replace("\n", " ").strip()


def extraer_datos_pdf(ruta_pdf):
    datos = {
        "numero_factura": "NO_ENCONTRADO",
        "fecha_emision": "NO_ENCONTRADO",
        "nit_emisor": "NO_ENCONTRADO",
        "detalles_producto": [],
    }

    try:
        with abrir_pdf(ruta_pdf) as pdf:
            texto = ""
            for page in pdf.pages:
                texto += (page.extract_text() or "") + "\n"

            num_factura = re.search(r"Número de Factura:\s*([A-Z0-9-]+)", texto)
            fecha_emision = re.search(r"Fecha de Emisión:\s*([\d/]+)", texto)
            nit_emisor = re.search(r"Nit del Emisor:\s*(\d+)", texto)

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

                    idx_header, cols = encontrar_indices_columnas(tabla)
                    if idx_header is None or cols is None:
                        continue

                    for fila in tabla[idx_header + 1 :]:
                        codigo = limpiar(fila[cols["codigo"]])
                        descripcion = limpiar(fila[cols["descripcion"]])
                        cantidad = limpiar(fila[cols["cantidad"]])
                        precio = limpiar(fila[cols["precio"]]).replace("$", "").strip()

                        descripcion = re.sub(r"\s+", " ", descripcion)

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
        print(f"   error leyendo {ruta_pdf.name}: {e}")

    return datos


def leer_pdfs():
    print("\nFase 2: leyendo los PDFs")

    t_inicio = time.perf_counter()
    archivos = sorted(DOWNLOADS_DIR.glob("*.pdf"))

    if not archivos:
        print(f"   no hay PDFs en {DOWNLOADS_DIR}")
        return 0.0, [], 0, 0

    filas = []
    tiempos_pdf = []
    sin_items = 0

    for idx, archivo in enumerate(archivos, start=1):
        t0 = time.perf_counter()
        datos = extraer_datos_pdf(archivo)
        duracion = time.perf_counter() - t0
        tiempos_pdf.append(duracion)

        fin_cufe = archivo.stem[-4:]
        items = datos["detalles_producto"]

        if not items:
            sin_items += 1

        for prod in items:
            filas.append(
                {
                    "CUFE_ending": fin_cufe,
                    "Numero_Factura": datos["numero_factura"],
                    "Fecha_Emision": datos["fecha_emision"],
                    "NIT_Emisor": datos["nit_emisor"],
                    "Codigo": prod["Codigo"],
                    "Descripcion": prod["Descripcion"],
                    "Cantidad": prod["Cantidad"],
                    "Precio_Unitario": prod["Precio_Unitario"],
                }
            )

        print(
            f"[{idx}/{len(archivos)}] ...{fin_cufe}  "
            f"factura {datos['numero_factura']}, {len(items)} items, {duracion:.2f} s"
        )

    t0_csv = time.perf_counter()
    if filas:
        df = pd.DataFrame(filas)
        df.to_csv(REPORTE_FINAL_CSV, index=False, sep=",", encoding="utf-8-sig")
        t_csv = time.perf_counter() - t0_csv
        print(f"\nCSV listo: {len(df)} filas en {REPORTE_FINAL_CSV} ({t_csv:.2f} s)")
    else:
        print("\nOjo: no salió ningún producto de los PDFs")

    t_fase2 = time.perf_counter() - t_inicio
    return t_fase2, tiempos_pdf, len(filas), sin_items


# ----------------------------------------------------------------- resumen ---


def imprimir_resumen(
    t_fase1, t_fase2, t_total, resultados, t_pausas, tiempos_pdf, total_items, sin_items
):
    tiempos = [m["tiempo_total_s"] for _, m in resultados]
    esperas = [m["espera_captcha_s"] for _, m in resultados]
    intentos = sum(m["intentos"] for _, m in resultados)
    primer_intento = sum(1 for e, m in resultados if e == "OK" and m["intentos"] == 1)
    ok = sum(1 for e, _ in resultados if e == "OK")
    fallos = len(resultados) - ok

    print("\n" + "-" * 60)
    print("RESUMEN")

    print("\nTiempos")
    print(f"  fase 1 (descarga):  {t_fase1:.1f} s ({t_fase1 / t_total * 100:.0f}%)")
    print(f"  fase 2 (leer pdfs): {t_fase2:.1f} s ({t_fase2 / t_total * 100:.0f}%)")
    print(f"  total:              {t_total:.1f} s")

    print(f"\nDescarga: {len(resultados)} facturas, {ok} ok, {fallos} con fallo")
    if tiempos:
        prom = statistics.mean(tiempos)
        desv = statistics.stdev(tiempos) if len(tiempos) > 1 else 0.0
        t_captcha = sum(esperas)
        print(
            f"  por factura: prom {prom:.1f} s, min {min(tiempos):.1f} s, "
            f"max {max(tiempos):.1f} s, desv {desv:.1f} s"
        )
        print(
            f"  bien al primer intento: {primer_intento}/{len(tiempos)} "
            f"({primer_intento / len(tiempos) * 100:.0f}%)"
        )
        print(
            f"  intentos en total: {intentos} "
            f"({intentos / len(tiempos):.2f} por factura)"
        )
        print(
            f"  esperando a 2captcha: {t_captcha:.1f} s "
            f"({t_captcha / t_fase1 * 100:.0f}% de la fase 1)"
        )
        print(
            f"  pausas entre facturas: {t_pausas:.1f} s "
            f"({t_pausas / t_fase1 * 100:.0f}% de la fase 1)"
        )

    print("\nPDFs")
    if tiempos_pdf:
        prom_pdf = statistics.mean(tiempos_pdf)
        velocidad = len(tiempos_pdf) / t_fase2 if t_fase2 > 0 else 0
        print(f"  leídos: {len(tiempos_pdf)} ({total_items} productos en total)")
        print(
            f"  por pdf: prom {prom_pdf:.3f} s, min {min(tiempos_pdf):.3f} s, "
            f"max {max(tiempos_pdf):.3f} s"
        )
        print(f"  velocidad: {velocidad:.2f} pdfs por segundo")
        print(f"  pdfs sin productos: {sin_items}")
    else:
        print("  no se leyó ninguno")

    print("\nConfiguración usada")
    print(
        f"  timeouts: descarga {TIMEOUT_DESCARGA_MS / 1000:.0f} s, "
        f"diálogo {TIMEOUT_DIALOGO_MS / 1000:.0f} s, resultado {TIMEOUT_RESULTADO_S} s"
    )
    print(
        f"  2captcha: {MAX_RESOLUCIONES_PARALELAS} hilos, precarga de "
        f"{VENTANA_PRECARGA} facturas, token válido {TOKEN_TTL_S} s"
    )
    print(
        f"  pausa entre facturas: {PAUSA_ENTRE_DOCS_S[0]} a {PAUSA_ENTRE_DOCS_S[1]} s"
    )
    print(f"  máximo {MAX_INTENTOS} intentos por factura")
    print("-" * 60 + "\n")


def main():
    t_inicio_global = time.perf_counter()

    print("Fase 1: descargando facturas de la DIAN")

    t_inicio_fase1 = time.perf_counter()
    cufes = leer_cufes()
    print(f"{len(cufes)} facturas en {CSV_ENTRADA.name}")

    resultados = []
    banco = BancoDeTokens()
    t_pausas = 0.0

    try:
        if cufes:
            with sync_playwright() as p:
                browser = lanzar_navegador(p)
                context = browser.new_context(
                    accept_downloads=True, locale="es-CO", timezone_id="America/Bogota"
                )
                page = context.new_page()
                try:
                    for i, (cufe, nit) in enumerate(cufes, start=1):
                        print(f"\n[{i}/{len(cufes)}] {cufe[:12]}... (NIT {nit})")
                        precargar(banco, cufes, i - 1)

                        estado, detalle, metricas = procesar_cufe(
                            page, banco, cufe, nit
                        )
                        if estado == "OK":
                            print(
                                f"   listo en {metricas['tiempo_total_s']:.1f} s "
                                f"(captcha {metricas['espera_captcha_s']:.1f} s, "
                                f"{metricas['intentos']} intento(s))"
                            )
                        else:
                            print(f"   no se pudo descargar: {detalle}")
                        resultados.append((estado, metricas))

                        if i < len(cufes):
                            pausa = random.uniform(*PAUSA_ENTRE_DOCS_S)
                            t_pausas += pausa
                            time.sleep(pausa)
                finally:
                    browser.close()
    finally:
        banco.cerrar()

    t_fase1 = time.perf_counter() - t_inicio_fase1

    t_fase2, tiempos_pdf, total_items, sin_items = leer_pdfs()

    t_total = time.perf_counter() - t_inicio_global
    imprimir_resumen(
        t_fase1,
        t_fase2,
        t_total,
        resultados,
        t_pausas,
        tiempos_pdf,
        total_items,
        sin_items,
    )


if __name__ == "__main__":
    main()
