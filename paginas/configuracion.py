"""Configuración y diagnóstico previo a una demostración.

Regla de esta página: **solo se muestra como control lo que de verdad hace algo.**
Los umbrales de detección se muestran en modo lectura porque hoy se leen desde
`config.py` al importar los módulos; ponerles un control deslizante que no
afecta nada sería exactamente el tipo de fachada que este proyecto evita.
"""

import os
import sqlite3

import streamlit as st

import config
from core import historial

st.title("Configuración")

# --- Ejecución ---
st.subheader("Ejecución")

izquierda, derecha = st.columns(2)

with izquierda:
    modelo = st.text_input(
        "Modelo local (Ollama) para el Investigador",
        value=st.session_state.get("modelo_ollama") or config.OLLAMA_MODEL,
        help="Se pasa tal cual al pipeline en la siguiente corrida.",
    )
    st.session_state["modelo_ollama"] = modelo.strip() or None

with derecha:
    st.session_state["guardar_historial"] = st.toggle(
        "Archivar cada expediente en el historial",
        value=st.session_state.get("guardar_historial", True),
        help=(
            "Los datos del caso (facturas, pagos, entidades) se reemplazan por completo "
            "en cada ingesta; eso es deliberado, porque mezclar dos empresas inventaría "
            "ciclos de dinero falsos. El historial guarda el expediente aparte, con sus "
            "leads verificados dentro, para que sobreviva a esa limpieza."
        ),
    )
    st.caption(f"Expedientes archivados hasta ahora: **{historial.contar_expedientes()}**")

st.divider()

# --- Diagnóstico ---
st.subheader("Diagnóstico")
st.caption("Revisa esto antes de una demostración en vivo.")


def _estado_ollama() -> tuple[bool, str]:
    try:
        import ollama
        cliente = ollama.Client(host=config.OLLAMA_HOST)
        modelos = [m.model for m in cliente.list().models]
    except Exception as exc:
        return False, f"No responde en {config.OLLAMA_HOST} ({type(exc).__name__})"
    objetivo = st.session_state.get("modelo_ollama") or config.OLLAMA_MODEL
    if objetivo in modelos:
        return True, f"Disponible · {objetivo} está descargado"
    return False, f"Responde, pero '{objetivo}' no está descargado. Hay: {', '.join(modelos[:4])}"


def _estado_gemini() -> tuple[bool, str]:
    clave = os.environ.get("GEMINI_API_KEY", "")
    if not clave:
        return False, "Falta GEMINI_API_KEY en el archivo .env"
    return True, f"Clave configurada · modelo {config.GEMINI_MODEL}"


def _estado_base() -> tuple[bool, str]:
    if not config.DB_PATH.exists():
        return False, "fraud.db todavía no existe (se crea con la primera ingesta)"
    try:
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        try:
            entidades = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
            lista = conn.execute("SELECT COUNT(*) FROM sat_blacklist_69b").fetchone()[0]
        finally:
            conn.close()
    except Exception as exc:
        return False, f"No se pudo leer: {exc}"
    return True, f"{entidades:,} entidades del caso · {lista:,} RFC en el catálogo 69-B"


for nombre, (ok, detalle) in {
    "Modelo local (Ollama)": _estado_ollama(),
    "Auditor (Gemini)": _estado_gemini(),
    "Base de datos": _estado_base(),
}.items():
    icono = "✅" if ok else "⚠️"
    st.markdown(f"{icono} **{nombre}** — {detalle}")

st.caption(
    "Si el Auditor falla, la investigación NO se pierde: el expediente se redacta con "
    "una plantilla determinista y se avisa en pantalla. Los montos e identificadores "
    "son los mismos porque no dependen del modelo."
)

st.divider()

# --- Umbrales (lectura) ---
st.subheader("Umbrales de detección")
st.caption(
    "Se muestran en modo lectura: se leen de `config.py` al arrancar. Para cambiarlos "
    "hay que editar ese archivo y reiniciar — se prefiere eso a un control que aparente "
    "hacer algo sin hacerlo."
)

st.dataframe(
    [
        {"Parámetro": "MIN_CICLO_MONTO", "Valor": f"${config.MIN_CICLO_MONTO:,.2f}",
         "Qué hace": "Monto mínimo del tramo más chico de un ciclo de dinero para tomarlo en cuenta."},
        {"Parámetro": "MAX_RATIO_MONTO_CICLO", "Valor": f"{config.MAX_RATIO_MONTO_CICLO}",
         "Qué hace": "Qué tanto pueden diferir los tramos de un ciclo. Sin esto, el comercio normal genera decenas de 'ciclos' falsos."},
        {"Parámetro": "MIN_DISCREPANCIA_PAGO", "Valor": f"${config.MIN_DISCREPANCIA_PAGO:,.2f}",
         "Qué hace": "Diferencia mínima entre lo facturado y lo pagado para marcar la factura."},
        {"Parámetro": "MIN_INGRESO_SIN_FACTURA", "Valor": f"${config.MIN_INGRESO_SIN_FACTURA:,.2f}",
         "Qué hace": "Monto mínimo de un depósito recibido sin CFDI para marcarlo como ingreso no declarado."},
        {"Parámetro": "MAX_ITERATIONS", "Valor": f"{config.MAX_ITERATIONS}",
         "Qué hace": "Turnos máximos del loop de herramientas del Investigador por cada RFC."},
        {"Parámetro": "CONTEXT_WINDOW", "Valor": f"{config.CONTEXT_WINDOW:,}",
         "Qué hace": "Ventana de contexto. Ollama usa 4096 por default, que truncaba las respuestas a media investigación."},
    ],
    hide_index=True, width="stretch",
)
