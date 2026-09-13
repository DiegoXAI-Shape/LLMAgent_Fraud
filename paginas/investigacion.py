"""Página principal: sube un caso, córrelo en vivo, y defiende el resultado.

Los resultados se guardan en `st.session_state` en cuanto el pipeline termina.
Eso NO es un detalle: Streamlit vuelve a ejecutar el script completo con cada
interacción, así que sin guardarlos, escribir una pregunta en el interrogatorio
dispararía otra investigación de 60 segundos y perdería el expediente anterior.
"""

import tempfile
from pathlib import Path

import streamlit as st

import config
from agents.defensor import PREGUNTAS_SUGERIDAS, responder_pregunta
from core.money_trail import construir_dot, leyenda
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
        "Archivo de entrada", type=EXTENSIONES_ACEPTADAS,
        help="Excel o CSV con el esquema completo, o un CFDI suelto en PDF/imagen.",
    )
    usar_demo = st.checkbox("Usar dataset de demostración", value=True,
                            help="27 empresas con los 5 esquemas de fraude sembrados.")
    rfc_manual = st.text_input(
        "Investigar un RFC específico", placeholder="opcional",
        help="Si se deja vacío, el sistema elige a quién investigar con sus propios detectores.",
    )
    st.divider()
    boton_correr = st.button("Iniciar investigación", type="primary", width="stretch")


def _resolver_entrada() -> Path | None:
    """El archivo subido manda sobre el de demostración."""
    if archivo_subido is not None:
        temporal = Path(tempfile.mkstemp(suffix=Path(archivo_subido.name).suffix)[1])
        temporal.write_bytes(archivo_subido.getvalue())
        return temporal
    if usar_demo:
        return config.EXCEL_PATH
    return None


def _filas_evidencia(evidencia: list[dict] | None) -> list[dict]:
    filas = []
    for item in evidencia or []:
        monto = item.get("monto")
        origen = item.get("emisor_rfc") or item.get("origen_rfc")
        destino = item.get("receptor_rfc") or item.get("destino_rfc")
        filas.append({
            "Instrumento": item.get("tipo", "—"),
            "Identificador": item.get("uuid") or item.get("tx_id") or "—",
            "De": origen or "—",
            "A": destino or "—",
            "Monto": f"${monto:,.2f}" if isinstance(monto, (int, float)) else "—",
            # Una factura y la transferencia que la paga son el mismo dinero: el pago
            # se cita como prueba pero no se suma. Sin esta columna, el total parecería
            # no cuadrar con la suma de las filas.
            "Cómputo": "Suma al total" if item.get("contado_en_total") else "Respaldo (no suma)",
        })
    return filas


def _tarjeta_confirmado(lead: dict) -> None:
    monto = lead.get("monto_total_evidencia") or 0.0
    with st.container(border=True):
        encabezado, etiqueta = st.columns([3, 1])
        encabezado.markdown(f"**{lead.get('razon_social') or 's/n'}**  \n`{lead.get('rfc_imputado')}`")
        etiqueta.markdown(f"`{lead.get('tipo_esquema')}`")
        st.markdown(f"## ${monto:,.2f} MXN")
        if lead.get("regla_fiscal"):
            st.caption(f"Regla violada: {lead['regla_fiscal']}")
        if lead.get("narrativa"):
            st.write(lead["narrativa"])

        dot = construir_dot(lead)
        if dot:
            st.markdown("**Rastro del dinero**")
            st.graphviz_chart(dot, width="stretch")
            st.caption(leyenda(lead))

        filas = _filas_evidencia(lead.get("evidencia"))
        if filas:
            st.dataframe(filas, hide_index=True, width="stretch")


# ---------------------------------------------------------------- corrida

if boton_correr:
    ruta_entrada = _resolver_entrada()
    if ruta_entrada is None:
        st.error("Sube un archivo o marca la casilla del dataset de demostración.")
        st.stop()

    # Una investigación nueva invalida la defensa de la anterior.
    st.session_state.pop("ultimo_caso", None)
    st.session_state["interrogatorio"] = []

    rfcs_filtro = [rfc_manual.strip().upper()] if rfc_manual.strip() else None
    modelo = st.session_state.get("modelo_ollama") or config.OLLAMA_MODEL

    estado_actual = st.empty()
    barra = st.progress(0.0)
    PESO_FASE = {"ingesta": 0.10, "investigador": 0.55, "verificador": 0.75,
                 "auditor": 0.95, "fin": 1.0}

    panel_ingesta = st.expander("**1 · Ingesta**", expanded=True)
    panel_investigador = st.expander("**2 · Investigador** — modelo local, ReAct con herramientas", expanded=True)
    panel_verificador = st.expander("**3 · Verificador determinista** — filtro de evidencia dura", expanded=True)
    panel_auditor = st.expander("**4 · Auditor** — expediente pericial", expanded=False)

    registro_investigador = panel_investigador.empty()
    rfcs_procesados: list[str] = []

    try:
        for evento in ejecutar_pipeline(
            ruta_entrada, rfcs=rfcs_filtro, model=modelo,
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
                    barra.empty(); estado_actual.empty()
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
                    registro_investigador.write("\n".join(f"- {l}" for l in rfcs_procesados))
                elif estado == "ok":
                    panel_investigador.success(f"{evento['datos']['n_borradores']} borradores generados.")

            elif fase == "verificador":
                if estado == "inicio":
                    estado_actual.info("Contrastando cada borrador contra la base de datos…")
                elif estado == "confirmado":
                    lead = evento["datos"]["lead"]
                    panel_verificador.success(
                        f"**CONFIRMADO** · `{lead.get('rfc_imputado')}` — {lead.get('tipo_esquema')}")
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
                barra.empty(); estado_actual.empty()
                datos = evento["datos"]
                # El PDF se construye una sola vez, aquí: rehacerlo en cada
                # interacción posterior costaría segundos por cada pregunta.
                try:
                    destino_pdf = config.ROOT_DIR / "expediente.pdf"
                    construir_pdf(
                        confirmados=datos["confirmados"], descartados=datos["descartados"],
                        destino=destino_pdf, archivo_origen=datos.get("archivo_origen"),
                        redactado_por=datos["redactado_por"],
                    )
                    datos["pdf_bytes"] = destino_pdf.read_bytes()
                except Exception as exc:
                    datos["pdf_error"] = str(exc)
                st.session_state["ultimo_caso"] = datos

    except Exception as exc:
        barra.empty(); estado_actual.empty()
        st.error("La investigación se interrumpió por un error inesperado.")
        st.exception(exc)


# ------------------------------------------------------------- resultados

caso = st.session_state.get("ultimo_caso")
if caso is None:
    st.info("Configura el caso en la barra lateral y presiona **Iniciar investigación**.")
    st.stop()

if caso["redactado_por"] == "plantilla":
    st.warning(
        "El Auditor (Gemini) no estuvo disponible, así que la redacción salió de la "
        "plantilla determinista. Los montos, identificadores y dictámenes son "
        "exactamente los mismos: lo único que cambia es la prosa.", icon="⚠️",
    )

st.divider()
c1, c2, c3, c4 = st.columns(4)
monto_total = sum(float(l.get("monto_total_evidencia") or 0) for l in caso["confirmados"])
c1.metric("Casos confirmados", caso["n_confirmados"])
c2.metric("Leads descartados", caso["n_descartados"])
c3.metric("Monto acreditado", f"${monto_total:,.0f}")
c4.metric("Tiempo total", f"{caso['elapsed_seconds']}s")

if caso.get("pdf_bytes"):
    st.download_button(
        "Descargar expediente en PDF", caso["pdf_bytes"],
        file_name="expediente.pdf", mime="application/pdf",
        type="primary", width="stretch",
    )
elif caso.get("pdf_error"):
    st.error(f"El expediente se generó, pero falló la exportación a PDF: {caso['pdf_error']}")

if caso.get("expediente_id"):
    st.caption(f"Archivado en el historial · `{caso['expediente_id']}`")

st.subheader("Casos confirmados")
if not caso["confirmados"]:
    st.write("Ninguno en esta corrida.")
for lead in caso["confirmados"]:
    _tarjeta_confirmado(lead)

if caso["descartados"]:
    st.subheader("Leads descartados")
    st.caption(
        "Se listan íntegros y con su razón exacta. Un expediente que solo enumera "
        "hallazgos esconde cuánto se revisó para llegar a ellos."
    )
    for item in caso["descartados"]:
        st.warning(f"`{item['lead'].get('rfc_imputado')}` — {item['razon']}", icon="⚠️")


# ---------------------------------------------------------- interrogatorio

st.divider()
st.subheader("Interrogatorio")
st.caption(
    "Pregúntale al agente por qué concluyó lo que concluyó. Responde solo desde el "
    "expediente y los registros: si no puede respaldar algo, lo dice en vez de inventarlo."
)

st.session_state.setdefault("interrogatorio", [])

with st.form("form_interrogatorio", clear_on_submit=True):
    pregunta = st.text_input(
        "Pregunta del auditor",
        placeholder="¿Por qué acusaste a esta empresa y no a otra?",
        label_visibility="collapsed",
    )
    preguntar = st.form_submit_button("Preguntar", type="primary")

st.caption("Sugerencias: " + " · ".join(f"*{p}*" for p in PREGUNTAS_SUGERIDAS[:3]))

if preguntar and pregunta.strip():
    with st.spinner("Revisando los registros para responder…"):
        try:
            resultado = responder_pregunta(
                pregunta=pregunta,
                confirmados=caso["confirmados"],
                descartados=caso["descartados"],
                model=st.session_state.get("modelo_ollama") or config.OLLAMA_MODEL,
            )
        except Exception as exc:
            resultado = {
                "respuesta": f"No se pudo consultar al modelo local: {exc}",
                "herramientas_usadas": [], "identificadores_inventados": [],
            }
    st.session_state["interrogatorio"].insert(0, {"pregunta": pregunta, **resultado})

for intercambio in st.session_state["interrogatorio"]:
    with st.container(border=True):
        st.markdown(f"**Auditor:** {intercambio['pregunta']}")
        st.markdown(intercambio["respuesta"])
        if intercambio.get("identificadores_inventados"):
            st.error(
                "Esta respuesta cita identificadores que no existen en el expediente ni en "
                f"la base: {', '.join(intercambio['identificadores_inventados'])}. "
                "Se muestra marcada en vez de ocultarla.", icon="🚨",
            )
        if intercambio.get("contradice_expediente"):
            st.error(
                "Esta respuesta parece negar una imputación que el expediente SÍ confirmó "
                f"({', '.join(intercambio['contradice_expediente'])}). El dictamen válido es "
                "el del expediente, no el de esta explicación.", icon="🚨",
            )
        if intercambio.get("contradice_registros"):
            st.error(
                "Esta respuesta declara una situación en el listado 69-B distinta a la "
                f"registrada: {'; '.join(intercambio['contradice_registros'])}. La "
                "distinción importa: sobre un PRESUNTO la imputación de EFOS no se sostiene.",
                icon="🚨",
            )
        if intercambio.get("herramientas_usadas"):
            with st.expander(f"Consultó {len(intercambio['herramientas_usadas'])} vez/veces la base"):
                for llamada in intercambio["herramientas_usadas"]:
                    st.code(llamada, language="text")
