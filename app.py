"""Frontend Streamlit: consume core.pipeline.ejecutar_pipeline y va llenando
la pantalla evento por evento, en vivo, mientras el pipeline corre.

Ni una sola línea de HTML/CSS/JS -- todo el frontend está en Python. La lógica
del pipeline (qué hace, en qué orden, qué significa cada resultado) vive
ÚNICAMENTE en core/pipeline.py; este archivo solo decide cómo pintar cada
evento en pantalla, igual que main.py decide cómo imprimirlo en terminal."""

import tempfile
import time
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

import config
from core.pipeline import ejecutar_pipeline

st.set_page_config(page_title="Agente Forense de Fraude Fiscal", page_icon="🔎", layout="wide")

EXTENSIONES_ACEPTADAS = ["xlsx", "xls", "csv", "pdf", "png", "jpg", "jpeg"]

st.title("🔎 Agente Forense de Fraude Fiscal")
st.caption("EFOS · CFDI · Artículo 69-B del CFF — detección, verificación y expediente pericial")

with st.sidebar:
    st.header("Entrada")
    archivo_subido = st.file_uploader(
        "Sube el caso a investigar",
        type=EXTENSIONES_ACEPTADAS,
        help="Excel/CSV con el esquema completo, o un CFDI suelto en PDF/imagen.",
    )
    usar_archivo_demo = st.checkbox(
        "Usar el dataset de demo (27 empresas, fraude sembrado)",
        value=archivo_subido is None,
    )
    rfc_manual = st.text_input(
        "RFC específico a investigar (opcional)",
        help="Si se deja vacío, el sistema decide a quién investigar con sus propios detectores.",
    )
    boton_correr = st.button("Iniciar investigación", type="primary", use_container_width=True)

    st.divider()
    st.caption(
        "El Investigador (Qwen local) solo señala identificadores. Todos los montos, "
        "razones sociales y conceptos se resuelven con SQL determinista contra la base "
        "-- ninguna cifra del expediente la genera un modelo de lenguaje."
    )


def _resolver_archivo_entrada() -> Path | None:
    if archivo_subido is not None:
        sufijo = Path(archivo_subido.name).suffix
        temporal = Path(tempfile.mkstemp(suffix=sufijo)[1])
        temporal.write_bytes(archivo_subido.getvalue())
        return temporal
    if usar_archivo_demo:
        return config.EXCEL_PATH
    return None


def _tabla_evidencia(evidencia: list[dict] | None) -> list[dict]:
    filas = []
    for item in evidencia or []:
        identificador = item.get("uuid") or item.get("tx_id") or "—"
        monto = item.get("monto")
        filas.append({
            "Tipo": item.get("tipo", "—"),
            "Identificador": identificador,
            "Monto": f"${monto:,.2f}" if isinstance(monto, (int, float)) else "—",
            "Suma al total": "sí" if item.get("contado_en_total") else "no (respaldo)",
        })
    return filas


def _pintar_confirmado(lead: dict) -> None:
    monto = lead.get("monto_total_evidencia") or 0.0
    with st.container(border=True):
        col1, col2 = st.columns([3, 1])
        col1.markdown(f"**{lead.get('rfc_imputado')}** — {lead.get('razon_social') or 's/n'}")
        col2.markdown(f"`{lead.get('tipo_esquema')}`")
        st.markdown(f"### ${monto:,.2f} MXN")
        filas = _tabla_evidencia(lead.get("evidencia"))
        if filas:
            st.dataframe(filas, hide_index=True, use_container_width=True)


def _pintar_descartado(lead: dict, razon: str) -> None:
    rfc = lead.get("rfc_imputado", "—")
    st.warning(f"**{rfc}** — {razon}", icon="⚠️")


if boton_correr:
    ruta_entrada = _resolver_archivo_entrada()
    if ruta_entrada is None:
        st.error("Sube un archivo o marca la casilla del dataset de demo.")
        st.stop()

    rfcs_filtro = [rfc_manual.strip().upper()] if rfc_manual.strip() else None

    fase_actual = st.empty()
    barra = st.progress(0.0)
    PESO_FASE = {"ingesta": 0.1, "investigador": 0.5, "verificador": 0.7, "auditor": 0.95, "fin": 1.0}

    contenedor_ingesta = st.expander("**[1/4] Ingesta**", expanded=True)
    contenedor_investigador = st.expander("**[2/4] Investigador** (Qwen local, ReAct + herramientas)", expanded=True)
    contenedor_verificador = st.expander("**[3/4] Verificador determinista**", expanded=True)
    contenedor_auditor = st.expander("**[4/4] Auditor** (expediente pericial)", expanded=True)

    log_investigador = contenedor_investigador.empty()
    rfcs_vistos: list[str] = []

    t0 = time.time()
    try:
        for evento in ejecutar_pipeline(ruta_entrada, rfcs=rfcs_filtro):
            fase, estado = evento["fase"], evento["estado"]
            barra.progress(PESO_FASE.get(fase, 0.0))

            if fase == "ingesta":
                if estado == "inicio":
                    fase_actual.info(f"Leyendo: {evento['mensaje']}")
                elif estado == "progreso":
                    contenedor_ingesta.write(evento["mensaje"])
                elif estado == "ok":
                    for tabla, n in evento["datos"]["counts"].items():
                        contenedor_ingesta.write(f"- `{tabla}`: {n} filas")
                elif estado == "error":
                    st.error(f"Error de ingesta: {evento['mensaje']}")
                    st.stop()

            elif fase == "investigador":
                if estado == "inicio":
                    rfcs_a_investigar = evento["datos"]["rfcs"]
                    fase_actual.info(f"Investigando {len(rfcs_a_investigar)} RFC(s): {', '.join(rfcs_a_investigar)}")
                elif estado == "progreso":
                    rfcs_vistos.append(evento["mensaje"])
                    log_investigador.write("\n".join(f"- {linea}" for linea in rfcs_vistos))
                elif estado == "ok":
                    contenedor_investigador.success(
                        f"{evento['datos']['n_borradores']} borradores generados."
                    )

            elif fase == "verificador":
                if estado == "inicio":
                    fase_actual.info("Verificando cada borrador contra la base de datos...")
                elif estado == "confirmado":
                    with contenedor_verificador:
                        st.success(f"CONFIRMADO: {evento['datos']['lead'].get('rfc_imputado')}")
                elif estado == "descartado":
                    with contenedor_verificador:
                        lead, razon = evento["datos"]["lead"], evento["datos"]["razon"]
                        st.warning(f"DESCARTADO: {lead.get('rfc_imputado')} — {razon}")

            elif fase == "auditor":
                if estado == "inicio":
                    fase_actual.info("Redactando expediente final...")
                elif estado == "ok":
                    contenedor_auditor.markdown(evento["datos"]["markdown"])
                    with open(evento["datos"]["output_path"], "rb") as fh:
                        contenedor_auditor.download_button(
                            "Descargar expediente (.md)", fh.read(),
                            file_name="CASE_FILE.md", mime="text/markdown",
                        )

            elif fase == "fin":
                d = evento["datos"]
                fase_actual.empty()
                barra.empty()

                col1, col2, col3 = st.columns(3)
                col1.metric("Confirmados", d["n_confirmados"])
                col2.metric("Descartados", d["n_descartados"])
                col3.metric("Tiempo total", f"{d['elapsed_seconds']}s")

                st.subheader("Casos confirmados")
                if not d["confirmados"]:
                    st.write("Ninguno.")
                for lead in d["confirmados"]:
                    _pintar_confirmado(lead)

                if d["descartados"]:
                    st.subheader("Leads descartados")
                    for item in d["descartados"]:
                        _pintar_descartado(item["lead"], item["razon"])

    except Exception as exc:
        st.exception(exc)
else:
    st.info("Configura la entrada en la barra lateral y presiona **Iniciar investigación**.")
