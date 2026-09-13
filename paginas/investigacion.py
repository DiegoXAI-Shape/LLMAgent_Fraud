"""Página principal: sube un caso, córrelo y ve las 4 fases en vivo."""

import tempfile
from pathlib import Path

import streamlit as st

import config
from core.pipeline import ejecutar_pipeline
from core.report_pdf import construir_pdf

EXTENSIONES_ACEPTADAS = ["xlsx", "xls", "csv", "pdf", "png", "jpg", "jpeg"]

st.title("Nueva investigación")
st.caption(
    "Artículo 69-B del CFF · El Investigador solo señala identificadores; cada monto y "
    "cada razón social se resuelven con SQL contra la base. Ninguna cifra del expediente "
    "la genera un modelo de lenguaje."
)

with st.sidebar:
    st.subheader("Caso a investigar")
    archivo_subido = st.file_uploader(
        "Archivo de entrada",
        type=EXTENSIONES_ACEPTADAS,
        help="Excel o CSV con el esquema completo, o un CFDI suelto en PDF/imagen.",
    )
    usar_demo = st.checkbox("Usar dataset de demostración", value=True,
                            help="27 empresas con los 5 esquemas de fraude sembrados.")
    rfc_manual = st.text_input(
        "Investigar un RFC específico",
        placeholder="opcional",
        help="Si se deja vacío, el sistema elige a quién investigar con sus propios detectores.",
    )
    st.divider()
    boton_correr = st.button("Iniciar investigación", type="primary", width="stretch")


def _resolver_entrada() -> Path | None:
    """El archivo subido manda sobre el de demostración."""
    if archivo_subido is not None:
        sufijo = Path(archivo_subido.name).suffix
        temporal = Path(tempfile.mkstemp(suffix=sufijo)[1])
        temporal.write_bytes(archivo_subido.getvalue())
        return temporal
    if usar_demo:
        return config.EXCEL_PATH
    return None


def _filas_evidencia(evidencia: list[dict] | None) -> list[dict]:
    filas = []
    for item in evidencia or []:
        monto = item.get("monto")
        filas.append({
            "Instrumento": item.get("tipo", "—"),
            "Identificador": item.get("uuid") or item.get("tx_id") or "—",
            "Monto": f"${monto:,.2f}" if isinstance(monto, (int, float)) else "—",
            # Una factura y la transferencia que la paga son el mismo dinero: el
            # pago se cita como prueba pero no se suma. Sin esta columna, el
            # total parecería no cuadrar con la suma de las filas.
            "Cómputo": "Suma al total" if item.get("contado_en_total") else "Respaldo (no suma)",
        })
    return filas


def _tarjeta_confirmado(lead: dict) -> None:
    monto = lead.get("monto_total_evidencia") or 0.0
    with st.container(border=True):
        encabezado, etiqueta = st.columns([3, 1])
        encabezado.markdown(
            f"**{lead.get('razon_social') or 's/n'}**  \n"
            f"`{lead.get('rfc_imputado')}`"
        )
        etiqueta.markdown(f"`{lead.get('tipo_esquema')}`")
        st.markdown(f"## ${monto:,.2f} MXN")
        if lead.get("regla_fiscal"):
            st.caption(f"Regla violada: {lead['regla_fiscal']}")
        if lead.get("narrativa"):
            st.write(lead["narrativa"])
        filas = _filas_evidencia(lead.get("evidencia"))
        if filas:
            st.dataframe(filas, hide_index=True, width="stretch")


if not boton_correr:
    st.info("Configura el caso en la barra lateral y presiona **Iniciar investigación**.")
    st.stop()

ruta_entrada = _resolver_entrada()
if ruta_entrada is None:
    st.error("Sube un archivo o marca la casilla del dataset de demostración.")
    st.stop()

rfcs_filtro = [rfc_manual.strip().upper()] if rfc_manual.strip() else None
modelo = st.session_state.get("modelo_ollama") or config.OLLAMA_MODEL

estado_actual = st.empty()
barra = st.progress(0.0)
PESO_FASE = {"ingesta": 0.10, "investigador": 0.55, "verificador": 0.75, "auditor": 0.95, "fin": 1.0}

panel_ingesta = st.expander("**1 · Ingesta**", expanded=True)
panel_investigador = st.expander("**2 · Investigador** — modelo local, ReAct con herramientas", expanded=True)
panel_verificador = st.expander("**3 · Verificador determinista** — filtro de evidencia dura", expanded=True)
panel_auditor = st.expander("**4 · Auditor** — expediente pericial", expanded=False)

registro_investigador = panel_investigador.empty()
rfcs_procesados: list[str] = []

try:
    for evento in ejecutar_pipeline(
        ruta_entrada,
        rfcs=rfcs_filtro,
        model=modelo,
        guardar_historial=st.session_state.get("guardar_historial", True),
    ):
        fase, estado = evento["fase"], evento["estado"]
        barra.progress(PESO_FASE.get(fase, 0.0))

        if fase == "ingesta":
            if estado == "inicio":
                estado_actual.info(f"Leyendo {Path(ruta_entrada).name}…")
            elif estado == "progreso":
                panel_ingesta.write(evento["mensaje"])
            elif estado == "ok":
                for tabla, n in evento["datos"]["counts"].items():
                    panel_ingesta.write(f"- `{tabla}`: {n:,} filas")
            elif estado == "error":
                barra.empty()
                estado_actual.empty()
                st.error(f"No se pudo ingerir el archivo: {evento['mensaje']}")
                st.stop()

        elif fase == "investigador":
            if estado == "inicio":
                lista = evento["datos"]["rfcs"]
                estado_actual.info(f"Investigando {len(lista)} RFC(s) con `{evento['datos']['model']}`…")
                panel_investigador.caption(
                    "Los detectores deterministas eligieron a estos candidatos escaneando "
                    "toda la base; no se le entregó una lista de sospechosos al modelo."
                )
            elif estado == "progreso":
                rfcs_procesados.append(evento["mensaje"])
                registro_investigador.write("\n".join(f"- {linea}" for linea in rfcs_procesados))
            elif estado == "ok":
                panel_investigador.success(f"{evento['datos']['n_borradores']} borradores generados.")

        elif fase == "verificador":
            if estado == "inicio":
                estado_actual.info("Contrastando cada borrador contra la base de datos…")
            elif estado == "confirmado":
                lead = evento["datos"]["lead"]
                panel_verificador.success(
                    f"**CONFIRMADO** · `{lead.get('rfc_imputado')}` — {lead.get('tipo_esquema')}"
                )
            elif estado == "descartado":
                lead, razon = evento["datos"]["lead"], evento["datos"]["razon"]
                panel_verificador.warning(f"**DESCARTADO** · `{lead.get('rfc_imputado')}` — {razon}")

        elif fase == "auditor":
            if estado == "inicio":
                estado_actual.info("Redactando el expediente pericial…")
            elif estado == "progreso":
                panel_auditor.warning(evento["mensaje"])
            elif estado == "ok":
                panel_auditor.markdown(evento["datos"]["markdown"])

        elif fase == "fin":
            datos = evento["datos"]
            barra.empty()
            estado_actual.empty()

            if datos["redactado_por"] == "plantilla":
                st.warning(
                    "El Auditor (Gemini) no estuvo disponible, así que la redacción salió de "
                    "la plantilla determinista. Los montos, identificadores y dictámenes son "
                    "exactamente los mismos: lo único que cambia es la prosa.",
                    icon="⚠️",
                )

            st.divider()
            c1, c2, c3, c4 = st.columns(4)
            monto_total = sum(float(l.get("monto_total_evidencia") or 0) for l in datos["confirmados"])
            c1.metric("Casos confirmados", datos["n_confirmados"])
            c2.metric("Leads descartados", datos["n_descartados"])
            c3.metric("Monto acreditado", f"${monto_total:,.0f}")
            c4.metric("Tiempo total", f"{datos['elapsed_seconds']}s")

            # El PDF se arma desde los leads verificados, no parseando el Markdown
            # del modelo: así el documento sale idéntico y correcto aunque la
            # redacción haya venido de la plantilla de respaldo.
            destino_pdf = config.ROOT_DIR / "expediente.pdf"
            try:
                construir_pdf(
                    confirmados=datos["confirmados"],
                    descartados=datos["descartados"],
                    destino=destino_pdf,
                    archivo_origen=datos.get("archivo_origen"),
                    redactado_por=datos["redactado_por"],
                )
                with open(destino_pdf, "rb") as fh:
                    st.download_button(
                        "Descargar expediente en PDF", fh.read(),
                        file_name=destino_pdf.name, mime="application/pdf",
                        type="primary", width="stretch",
                    )
            except Exception as exc:
                st.error(f"El expediente se generó, pero falló la exportación a PDF: {exc}")

            if datos.get("expediente_id"):
                st.caption(f"Archivado en el historial · `{datos['expediente_id']}`")

            st.subheader("Casos confirmados")
            if not datos["confirmados"]:
                st.write("Ninguno en esta corrida.")
            for lead in datos["confirmados"]:
                _tarjeta_confirmado(lead)

            if datos["descartados"]:
                st.subheader("Leads descartados")
                st.caption(
                    "Se listan íntegros y con su razón exacta. Un expediente que solo "
                    "enumera hallazgos esconde cuánto se revisó para llegar a ellos."
                )
                for item in datos["descartados"]:
                    st.warning(
                        f"`{item['lead'].get('rfc_imputado')}` — {item['razon']}", icon="⚠️"
                    )

except Exception as exc:
    barra.empty()
    estado_actual.empty()
    st.error("La investigación se interrumpió por un error inesperado.")
    st.exception(exc)
