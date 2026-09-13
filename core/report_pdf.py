"""Expediente pericial en PDF.

DECISIÓN CENTRAL: el PDF NO se arma parseando el Markdown que escribió Gemini.
Se arma desde los leads YA VERIFICADOS -- los mismos diccionarios que salieron
de `verifier.py` con cada monto e identificador resueltos por SQL contra
`fraud.db`. De Gemini se toma únicamente la prosa narrativa.

Dos razones, y las dos importan:

1. **Corrección.** Es el mismo principio que gobierna todo el proyecto: ninguna
   cifra del expediente la genera un modelo de lenguaje. Si el PDF se armara
   leyendo el Markdown, cualquier error de formato del modelo se convertiría en
   un error en el documento firmado. Aquí los montos y los UUID vienen del
   diccionario verificado, no de un texto.

2. **Consistencia visual.** Un LLM formatea distinto en cada corrida (a veces
   `###`, a veces `**`, a veces una tabla, a veces una lista). Parsear eso
   produciría un PDF que se ve diferente cada vez. Con los datos estructurados,
   el documento sale idéntico y profesional siempre, y da igual si lo redactó
   Gemini o la plantilla de respaldo.

El resultado es un expediente que se sostiene solo aunque Gemini esté caído: sin
él se pierde la prosa, no las pruebas.
"""

import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any

from fpdf import FPDF
from fpdf.enums import Align, XPos, YPos

# Paleta alineada con el tema de la interfaz (.streamlit/config.toml): azul
# pizarra sobrio para la estructura, rojo reservado SOLO para hallazgos
# confirmados, para que el color signifique algo y no sea decoración.
AZUL = (31, 78, 121)
AZUL_CLARO = (232, 238, 245)
GRIS_TEXTO = (27, 39, 51)
GRIS_SUAVE = (110, 122, 134)
ROJO = (169, 45, 45)
VERDE = (34, 110, 70)

# Windows trae estas fuentes siempre. Si el repo se clona en otro sistema
# operativo y no está ninguna, se cae a la fuente interna de fpdf2 (Helvetica),
# que no es Unicode -- por eso `_sanear` limpia el texto en ese caso. El PDF
# sale igual de válido, solo con tipografía distinta.
_CANDIDATAS_FUENTE = [
    (Path(r"C:\Windows\Fonts\arial.ttf"), Path(r"C:\Windows\Fonts\arialbd.ttf")),
    (Path(r"C:\Windows\Fonts\calibri.ttf"), Path(r"C:\Windows\Fonts\calibrib.ttf")),
    (Path(r"C:\Windows\Fonts\segoeui.ttf"), Path(r"C:\Windows\Fonts\segoeuib.ttf")),
    (Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"),
     Path("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")),
]

_REEMPLAZOS_ASCII = {
    "—": "-", "–": "-", "’": "'", "‘": "'", "“": '"', "”": '"',
    "…": "...", "·": "-", "→": "->", "≥": ">=", "≤": "<=", "•": "-",
}


def _sanear(texto: str) -> str:
    """Quita lo que la fuente interna (latin-1) no puede representar."""
    for original, reemplazo in _REEMPLAZOS_ASCII.items():
        texto = texto.replace(original, reemplazo)
    normalizado = unicodedata.normalize("NFC", texto)
    return normalizado.encode("latin-1", errors="replace").decode("latin-1")


def _moneda(valor: Any) -> str:
    try:
        return f"${float(valor):,.2f}"
    except (TypeError, ValueError):
        return "-"


class _Expediente(FPDF):
    """Documento con encabezado y pie de página consistentes en todas las hojas."""

    def __init__(self, folio: str, unicode_ok: bool):
        super().__init__(orientation="P", unit="mm", format="A4")
        self.folio = folio
        self.unicode_ok = unicode_ok
        self.fuente = "Documento" if unicode_ok else "Helvetica"
        self.set_auto_page_break(auto=True, margin=20)
        self.set_margins(left=18, top=16, right=18)

    def t(self, texto: str) -> str:
        """Texto listo para imprimir, según si la fuente soporta Unicode."""
        return texto if self.unicode_ok else _sanear(texto)

    def header(self) -> None:
        # La portada lleva su propio diseño; no se le encima el encabezado.
        if self.page_no() == 1:
            return
        self.set_font(self.fuente, "B", 8)
        self.set_text_color(*GRIS_SUAVE)
        self.cell(0, 5, self.t("EXPEDIENTE PERICIAL DE FRAUDE FISCAL"), align=Align.L)
        self.cell(0, 5, self.t(f"Folio {self.folio}"), align=Align.R,
                  new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*AZUL_CLARO)
        self.set_line_width(0.4)
        self.line(self.l_margin, self.get_y() + 1, self.w - self.r_margin, self.get_y() + 1)
        self.ln(5)

    def footer(self) -> None:
        self.set_y(-15)
        self.set_font(self.fuente, "", 7)
        self.set_text_color(*GRIS_SUAVE)
        leyenda = ("Documento generado automáticamente. Los montos e identificadores "
                   "provienen de verificación determinista contra la base de datos.")
        self.cell(0, 4, self.t(leyenda), align=Align.L)
        self.cell(0, 4, self.t(f"Página {self.page_no()}"), align=Align.R)

    # --- Bloques de contenido ---

    def titulo_seccion(self, texto: str) -> None:
        self.ln(2)
        self.set_font(self.fuente, "B", 12)
        self.set_text_color(*AZUL)
        self.cell(0, 7, self.t(texto), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.set_draw_color(*AZUL)
        self.set_line_width(0.5)
        self.line(self.l_margin, self.get_y(), self.l_margin + 40, self.get_y())
        self.ln(4)

    def parrafo(self, texto: str, tamano: int = 9.5) -> None:
        self.set_font(self.fuente, "", tamano)
        self.set_text_color(*GRIS_TEXTO)
        self.multi_cell(0, 5, self.t(texto), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        self.ln(1)

    def etiqueta_valor(self, etiqueta: str, valor: str) -> None:
        self.set_font(self.fuente, "B", 9)
        self.set_text_color(*GRIS_SUAVE)
        self.cell(42, 5.5, self.t(etiqueta))
        self.set_font(self.fuente, "", 9)
        self.set_text_color(*GRIS_TEXTO)
        self.multi_cell(0, 5.5, self.t(valor), new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _portada(pdf: _Expediente, resumen: dict) -> None:
    pdf.add_page()
    pdf.ln(28)

    pdf.set_font(pdf.fuente, "B", 22)
    pdf.set_text_color(*AZUL)
    pdf.multi_cell(0, 10, pdf.t("Expediente pericial\nde fraude fiscal"),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font(pdf.fuente, "", 10)
    pdf.set_text_color(*GRIS_SUAVE)
    pdf.multi_cell(0, 5.5, pdf.t(
        "Artículo 69-B del Código Fiscal de la Federación · Operaciones simuladas (EFOS)"),
        new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.ln(10)
    pdf.set_draw_color(*AZUL)
    pdf.set_line_width(0.8)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.l_margin + 55, pdf.get_y())
    pdf.ln(12)

    pdf.etiqueta_valor("Folio del expediente", resumen["folio"])
    pdf.etiqueta_valor("Fecha de emisión", resumen["fecha"])
    pdf.etiqueta_valor("Origen de los datos", resumen.get("archivo_origen") or "no especificado")
    pdf.etiqueta_valor("Redacción narrativa", resumen["redactado_por"])

    pdf.ln(10)

    # Recuadro con las cifras del dictamen.
    y_inicio = pdf.get_y()
    pdf.set_fill_color(*AZUL_CLARO)
    pdf.rect(pdf.l_margin, y_inicio, pdf.w - pdf.l_margin - pdf.r_margin, 30, style="F")
    pdf.set_y(y_inicio + 6)

    ancho = (pdf.w - pdf.l_margin - pdf.r_margin) / 3
    for etiqueta, valor, color in [
        ("Casos confirmados", str(resumen["n_confirmados"]), ROJO),
        ("Leads descartados", str(resumen["n_descartados"]), GRIS_SUAVE),
        ("Monto acreditado", _moneda(resumen["monto_total"]), AZUL),
    ]:
        x = pdf.get_x()
        pdf.set_font(pdf.fuente, "B", 16)
        pdf.set_text_color(*color)
        pdf.cell(ancho, 9, pdf.t(valor), align=Align.C)
        pdf.set_xy(x, pdf.get_y() + 9)
        pdf.set_font(pdf.fuente, "", 8)
        pdf.set_text_color(*GRIS_SUAVE)
        pdf.cell(ancho, 5, pdf.t(etiqueta), align=Align.C)
        pdf.set_xy(x + ancho, y_inicio + 6)

    pdf.set_y(y_inicio + 40)

    pdf.set_font(pdf.fuente, "", 8.5)
    pdf.set_text_color(*GRIS_SUAVE)
    pdf.multi_cell(0, 4.5, pdf.t(
        "Alcance y límites de este documento. Los hallazgos aquí consignados fueron "
        "confirmados por un proceso de verificación determinista que contrastó cada "
        "identificador y cada monto contra los registros de origen; ningún importe, RFC "
        "ni folio fiscal de este expediente fue redactado por un modelo de lenguaje. Los "
        "leads descartados se incluyen íntegros, con su razón de exoneración, para dejar "
        "constancia de lo que el sistema revisó y decidió NO imputar. Este documento "
        "constituye un insumo técnico de investigación y no determina responsabilidad "
        "fiscal o penal, la cual corresponde exclusivamente a la autoridad competente."),
        new_x=XPos.LMARGIN, new_y=YPos.NEXT)


def _tabla_resumen(pdf: _Expediente, confirmados: list[dict]) -> None:
    if not confirmados:
        return
    pdf.add_page()
    pdf.titulo_seccion("Resumen de casos confirmados")

    encabezados = ["#", "RFC imputado", "Razón social", "Tipología", "Monto"]
    anchos = [8, 30, 55, 42, 39]

    pdf.set_font(pdf.fuente, "B", 8.5)
    pdf.set_fill_color(*AZUL)
    pdf.set_text_color(255, 255, 255)
    for encabezado, ancho in zip(encabezados, anchos):
        alineacion = Align.R if encabezado == "Monto" else Align.L
        pdf.cell(ancho, 7, pdf.t(f" {encabezado}"), fill=True, align=alineacion)
    pdf.ln()

    pdf.set_font(pdf.fuente, "", 8.5)
    for indice, lead in enumerate(confirmados, start=1):
        relleno = indice % 2 == 0
        if relleno:
            pdf.set_fill_color(246, 248, 251)
        pdf.set_text_color(*GRIS_TEXTO)
        valores = [
            str(indice),
            str(lead.get("rfc_imputado") or "-"),
            str(lead.get("razon_social") or "-")[:34],
            str(lead.get("tipo_esquema") or "-"),
            _moneda(lead.get("monto_total_evidencia")),
        ]
        for valor, ancho, encabezado in zip(valores, anchos, encabezados):
            alineacion = Align.R if encabezado == "Monto" else Align.L
            pdf.cell(ancho, 6.5, pdf.t(f" {valor}"), fill=relleno, align=alineacion)
        pdf.ln()

    pdf.set_font(pdf.fuente, "B", 9)
    pdf.set_text_color(*AZUL)
    total = sum(float(l.get("monto_total_evidencia") or 0) for l in confirmados)
    pdf.cell(sum(anchos[:-1]), 8, pdf.t(" Total acreditado"), align=Align.R)
    pdf.cell(anchos[-1], 8, pdf.t(_moneda(total)), align=Align.R)
    pdf.ln()


def _tabla_evidencia(pdf: _Expediente, evidencia: list[dict]) -> None:
    if not evidencia:
        pdf.parrafo("(Sin partidas de evidencia registradas.)", tamano=8.5)
        return

    encabezados = ["Instrumento", "Identificador", "Monto", "Cómputo"]
    anchos = [26, 82, 32, 34]

    pdf.set_font(pdf.fuente, "B", 8)
    pdf.set_fill_color(*AZUL_CLARO)
    pdf.set_text_color(*AZUL)
    for encabezado, ancho in zip(encabezados, anchos):
        pdf.cell(ancho, 6, pdf.t(f" {encabezado}"), fill=True,
                 align=Align.R if encabezado == "Monto" else Align.L)
    pdf.ln()

    pdf.set_font(pdf.fuente, "", 7.5)
    pdf.set_text_color(*GRIS_TEXTO)
    for item in evidencia:
        identificador = item.get("uuid") or item.get("tx_id") or "-"
        # "Suma / respaldo" no es adorno: una factura y la transferencia que la
        # paga son el MISMO dinero, así que el pago se cita como prueba pero no
        # se suma al monto. El lector tiene que poder ver por qué el total no es
        # la suma aritmética de todas las filas.
        computo = "Suma al total" if item.get("contado_en_total") else "Respaldo (no suma)"
        valores = [str(item.get("tipo") or "-"), str(identificador),
                   _moneda(item.get("monto")), computo]
        for valor, ancho, encabezado in zip(valores, anchos, encabezados):
            pdf.cell(ancho, 5.5, pdf.t(f" {valor}"), border="B",
                     align=Align.R if encabezado == "Monto" else Align.L)
        pdf.ln()
    pdf.ln(2)


def _seccion_caso(pdf: _Expediente, indice: int, lead: dict) -> None:
    pdf.add_page()

    pdf.set_font(pdf.fuente, "B", 8)
    pdf.set_text_color(*ROJO)
    pdf.cell(0, 5, pdf.t(f"CASO {indice:02d} · CONFIRMADO CON PRUEBA"),
             new_x=XPos.LMARGIN, new_y=YPos.NEXT)

    pdf.set_font(pdf.fuente, "B", 15)
    pdf.set_text_color(*AZUL)
    pdf.multi_cell(0, 7.5, pdf.t(str(lead.get("razon_social") or lead.get("rfc_imputado") or "-")),
                   new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.ln(3)

    pdf.etiqueta_valor("RFC imputado", str(lead.get("rfc_imputado") or "-"))
    pdf.etiqueta_valor("Tipología del esquema", str(lead.get("tipo_esquema") or "-"))
    pdf.etiqueta_valor("Regla fiscal violada", str(lead.get("regla_fiscal") or "No especificada"))
    pdf.ln(3)

    monto = lead.get("monto_total_evidencia")
    y = pdf.get_y()
    pdf.set_fill_color(253, 243, 243)
    pdf.rect(pdf.l_margin, y, pdf.w - pdf.l_margin - pdf.r_margin, 16, style="F")
    pdf.set_xy(pdf.l_margin + 4, y + 3)
    pdf.set_font(pdf.fuente, "", 8)
    pdf.set_text_color(*GRIS_SUAVE)
    pdf.cell(0, 4, pdf.t("MONTO TOTAL ACREDITADO"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_x(pdf.l_margin + 4)
    pdf.set_font(pdf.fuente, "B", 15)
    pdf.set_text_color(*ROJO)
    pdf.cell(0, 7, pdf.t(f"{_moneda(monto)} MXN"), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
    pdf.set_y(y + 22)

    narrativa = (lead.get("narrativa") or "").strip()
    if narrativa:
        pdf.titulo_seccion("Hechos determinados")
        pdf.parrafo(narrativa)

    pdf.titulo_seccion("Cadena de evidencia")
    _tabla_evidencia(pdf, lead.get("evidencia") or [])


def _seccion_descartados(pdf: _Expediente, descartados: list[dict]) -> None:
    pdf.add_page()
    pdf.titulo_seccion("Leads descartados por falta de evidencia")
    pdf.parrafo(
        "Estos RFC fueron revisados por el sistema y NO se imputan. Se consignan "
        "íntegros, con la razón exacta por la que la evidencia no alcanzó el "
        "estándar de prueba exigido. Su inclusión es deliberada: un expediente que "
        "solo enumera hallazgos oculta cuánto se revisó para llegar a ellos.",
        tamano=9,
    )
    pdf.ln(2)

    if not descartados:
        pdf.parrafo("No hubo leads descartados en esta corrida.", tamano=9)
        return

    for item in descartados:
        lead = item.get("lead") or {}
        razon = item.get("razon") or "-"
        pdf.set_font(pdf.fuente, "B", 9.5)
        pdf.set_text_color(*GRIS_TEXTO)
        esquema = lead.get("tipo_esquema") or "sin tipificar"
        pdf.cell(0, 6, pdf.t(f"{lead.get('rfc_imputado') or '-'}  ·  tentativa: {esquema}"),
                 new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.set_font(pdf.fuente, "", 8.5)
        pdf.set_text_color(*GRIS_SUAVE)
        pdf.multi_cell(0, 4.8, pdf.t(razon), new_x=XPos.LMARGIN, new_y=YPos.NEXT)
        pdf.ln(3)


def construir_pdf(
    confirmados: list[dict[str, Any]],
    descartados: list[dict[str, Any]],
    destino: Path,
    archivo_origen: str | None = None,
    redactado_por: str = "gemini",
    folio: str | None = None,
) -> Path:
    """Escribe el expediente en PDF y devuelve la ruta.

    `descartados` se espera como lista de {"lead": dict, "razon": str}, que es
    justo la forma que emite el evento "fin" de `core.pipeline`.
    """
    ahora = datetime.now()
    folio = folio or ahora.strftime("EXP-%Y%m%d-%H%M")

    unicode_ok = False
    pdf = _Expediente(folio=folio, unicode_ok=False)
    for regular, negrita in _CANDIDATAS_FUENTE:
        if regular.exists() and negrita.exists():
            pdf.add_font("Documento", "", str(regular))
            pdf.add_font("Documento", "B", str(negrita))
            unicode_ok = True
            break
    pdf.unicode_ok = unicode_ok
    pdf.fuente = "Documento" if unicode_ok else "Helvetica"

    monto_total = sum(float(l.get("monto_total_evidencia") or 0) for l in confirmados)
    etiqueta_redaccion = {
        "gemini": "Auditor asistido por modelo (Gemini, temperatura 0)",
        "plantilla": "Plantilla determinista (el Auditor no estuvo disponible)",
    }.get(redactado_por, redactado_por)

    _portada(pdf, {
        "folio": folio,
        "fecha": ahora.strftime("%d/%m/%Y %H:%M"),
        "archivo_origen": archivo_origen,
        "redactado_por": etiqueta_redaccion,
        "n_confirmados": len(confirmados),
        "n_descartados": len(descartados),
        "monto_total": monto_total,
    })

    _tabla_resumen(pdf, confirmados)
    for indice, lead in enumerate(confirmados, start=1):
        _seccion_caso(pdf, indice, lead)
    _seccion_descartados(pdf, descartados)

    destino = Path(destino)
    destino.parent.mkdir(parents=True, exist_ok=True)
    pdf.output(str(destino))
    return destino
