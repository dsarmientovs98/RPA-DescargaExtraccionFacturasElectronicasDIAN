import os
import re
from pathlib import Path

import pandas as pd
import pdfplumber
from dotenv import load_dotenv

load_dotenv()
PASSWORD = os.getenv("PDF_PASSWORD")

CUFES_DIR = Path("/Users/danielsarmientovanegas/Desktop/Auto/Analista_Innovacion/CUFES")
OUTPUT_DIR = Path(__file__).parent / "data" / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def encontrar_indices_columnas(tabla):
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


def extraer_datos_pdf(ruta_pdf):
    datos = {
        "numero_factura": "NO_ENCONTRADO",
        "fecha_emision": "NO_ENCONTRADO",
        "nit_emisor": "NO_ENCONTRADO",
        "detalles_producto": [],
    }

    try:
        with pdfplumber.open(ruta_pdf, password=PASSWORD) as pdf:
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
                tablas = page.extract_tables()
                for tabla in tablas:
                    if not tabla:
                        continue

                    fila_header_idx, idx_cols = encontrar_indices_columnas(tabla)

                    if fila_header_idx is None or idx_cols is None:
                        continue

                    for fila in tabla[fila_header_idx + 1 :]:
                        codigo = (
                            str(fila[idx_cols["codigo"]] or "")
                            .replace("\n", " ")
                            .strip()
                        )
                        descripcion = (
                            str(fila[idx_cols["descripcion"]] or "")
                            .replace("\n", " ")
                            .strip()
                        )
                        cantidad = (
                            str(fila[idx_cols["cantidad"]] or "")
                            .replace("\n", " ")
                            .strip()
                        )
                        precio = (
                            str(fila[idx_cols["precio"]] or "")
                            .replace("\n", " ")
                            .replace("$", "")
                            .strip()
                        )

                        descripcion = re.sub(r"\s+", " ", descripcion)

                        # CORRECCIÓN: Quitamos la exigencia de que 'codigo' tenga texto.
                        # Solo exigimos que haya una cantidad numérica (y una descripción).
                        if re.match(r"^\d+[\.,]?\d*$", cantidad) and descripcion:
                            datos["detalles_producto"].append(
                                {
                                    "Codigo": codigo,
                                    "Descripcion": descripcion,
                                    "Cantidad": cantidad,
                                    "Precio_Unitario": precio,
                                }
                            )

    except Exception as e:  # noqa: BLE001
        print(f"Error crítico al leer {ruta_pdf.name}: {e}")

    return datos


def procesar_lote_cufes():
    archivos_pdf = list(CUFES_DIR.rglob("*.pdf"))
    if not archivos_pdf:
        return

    todas_las_facturas = []

    for archivo in archivos_pdf:
        datos_factura = extraer_datos_pdf(archivo)
        cufe_ending = archivo.stem[-4:]

        for prod in datos_factura["detalles_producto"]:
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

    if todas_las_facturas:
        df = pd.DataFrame(todas_las_facturas)
        ruta_csv = OUTPUT_DIR / "reporte_facturas.csv"
        df.to_csv(ruta_csv, index=False, sep=",", encoding="utf-8-sig")
        print(f"¡Éxito! CSV exportado con {len(df)} productos reales.")


if __name__ == "__main__":
    procesar_lote_cufes()
