"""Historial de expedientes: consulta corridas anteriores y reimprime su PDF.

Esta página existe porque `investigation_cases` se borra con cada ingesta nueva
(vive en el esquema del caso). El historial guarda los leads verificados
serializados, así que un expediente se puede volver a imprimir aunque los datos
del caso original ya no estén en la base.
"""

from pathlib import Path

import streamlit as st

import config
from core import historial
from core.report_pdf import construir_pdf

st.title("Expedientes")
st.caption(
    "Corridas archivadas. Sobreviven a las ingestas nuevas: se guardan en el esquema "
    "de referencia, el mismo que protege al catálogo del SAT."
)

expedientes = historial.listar_expedientes(limite=200)

if not expedientes:
    st.info(
        "Todavía no hay expedientes archivados. Corre una investigación con la opción "
        "de historial activada (está encendida por default en **Configuración**)."
    )
    st.stop()

total_acreditado = sum(float(e["monto_total"] or 0) for e in expedientes)
c1, c2, c3 = st.columns(3)
c1.metric("Expedientes archivados", len(expedientes))
c2.metric("Casos confirmados (total)", sum(e["n_confirmados"] for e in expedientes))
c3.metric("Monto acumulado", f"${total_acreditado:,.0f}")

st.divider()

filas = [{
    "Fecha": e["fecha_hora"],
    "Origen": e["archivo_origen"] or "—",
    "Confirmados": e["n_confirmados"],
    "Descartados": e["n_descartados"],
    "Monto": f"${float(e['monto_total'] or 0):,.2f}",
    "Redacción": e["redactado_por"],
} for e in expedientes]
st.dataframe(filas, hide_index=True, width="stretch")

st.subheader("Abrir un expediente")

etiquetas = {
    e["expediente_id"]: (
        f"{e['fecha_hora']} · {e['n_confirmados']} confirmado(s) · "
        f"${float(e['monto_total'] or 0):,.2f} · {e['archivo_origen'] or 's/n'}"
    )
    for e in expedientes
}
seleccionado = st.selectbox(
    "Expediente", options=list(etiquetas), format_func=lambda i: etiquetas[i], label_visibility="collapsed"
)

expediente = historial.obtener_expediente(seleccionado)
if expediente is None:
    st.error("Ese expediente ya no existe.")
    st.stop()

if expediente["redactado_por"] == "plantilla":
    st.warning(
        "Este expediente se redactó con la plantilla determinista porque el Auditor "
        "no estuvo disponible en esa corrida. Los hechos verificados son los mismos.",
        icon="⚠️",
    )

acciones_pdf, acciones_borrar = st.columns([3, 1])

with acciones_pdf:
    try:
        destino = config.ROOT_DIR / f"expediente_{seleccionado[:8]}.pdf"
        construir_pdf(
            confirmados=expediente["confirmados"],
            descartados=expediente["descartados"],
            destino=destino,
            archivo_origen=expediente["archivo_origen"],
            redactado_por=expediente["redactado_por"],
        )
        with open(destino, "rb") as fh:
            st.download_button(
                "Descargar este expediente en PDF", fh.read(),
                file_name=destino.name, mime="application/pdf",
                type="primary", width="stretch",
            )
    except Exception as exc:
        st.error(f"No se pudo reimprimir el PDF: {exc}")

with acciones_borrar:
    if st.button("Eliminar", width="stretch"):
        historial.borrar_expediente(seleccionado)
        st.rerun()

tab_resumen, tab_markdown = st.tabs(["Resumen", "Expediente redactado"])

with tab_resumen:
    confirmados = expediente["confirmados"]
    if not confirmados:
        st.write("Esta corrida no confirmó ningún caso.")
    for lead in confirmados:
        with st.container(border=True):
            st.markdown(
                f"**{lead.get('razon_social') or 's/n'}** · `{lead.get('rfc_imputado')}` · "
                f"`{lead.get('tipo_esquema')}`"
            )
            st.markdown(f"### ${float(lead.get('monto_total_evidencia') or 0):,.2f} MXN")
            evidencia = [{
                "Instrumento": item.get("tipo", "—"),
                "Identificador": item.get("uuid") or item.get("tx_id") or "—",
                "Monto": f"${item['monto']:,.2f}" if isinstance(item.get("monto"), (int, float)) else "—",
                "Cómputo": "Suma al total" if item.get("contado_en_total") else "Respaldo (no suma)",
            } for item in lead.get("evidencia") or []]
            if evidencia:
                st.dataframe(evidencia, hide_index=True, width="stretch")

    if expediente["descartados"]:
        st.subheader("Leads descartados")
        for item in expediente["descartados"]:
            st.warning(f"`{item['lead'].get('rfc_imputado')}` — {item['razon']}", icon="⚠️")

with tab_markdown:
    st.markdown(expediente["markdown"])
