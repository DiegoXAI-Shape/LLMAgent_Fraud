"""Punto de entrada del frontend. Define la navegación entre páginas y el
estilo visual común; el contenido de cada página vive en `paginas/`.

La lógica del pipeline NO vive aquí ni en ninguna página: está en
`core/pipeline.py`, y tanto este frontend como `main.py` (terminal) la consumen
como un generador de eventos. Las páginas solo deciden cómo pintar lo que ese
generador va entregando.
"""

import streamlit as st
from dotenv import load_dotenv

load_dotenv()

st.set_page_config(
    page_title="Agente Forense de Fraude Fiscal",
    page_icon="⚖️",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Ajustes finos que el archivo de tema (.streamlit/config.toml) no cubre. Es el
# único CSS del proyecto y es deliberadamente corto: quita el cromo de Streamlit
# que delata "esto es un demo" y aprieta el espaciado, que por default está
# calibrado para notebooks, no para un informe.
st.markdown("""
<style>
  #MainMenu, footer, header [data-testid="stStatusWidget"] { visibility: hidden; }
  .block-container { padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1180px; }
  h1, h2, h3 { letter-spacing: -0.01em; }
  h1 { font-size: 1.9rem !important; font-weight: 700 !important; }
  [data-testid="stMetricValue"] { font-size: 1.7rem; font-weight: 700; }
  [data-testid="stMetricLabel"] { font-size: 0.78rem; text-transform: uppercase;
                                   letter-spacing: 0.06em; color: #6E7A86; }
  [data-testid="stSidebarNav"] { padding-top: 0.5rem; }
  div[data-testid="stExpander"] details { border-radius: 8px; border-color: #E3E8EF; }
  .stDataFrame { border-radius: 8px; }
  hr { margin: 1.2rem 0; border-color: #E3E8EF; }
</style>
""", unsafe_allow_html=True)


# Valores que se comparten entre páginas. Se inicializan una sola vez aquí para
# que la página de Configuración y la de Investigación lean exactamente lo
# mismo, sin que importe en cuál entró el usuario primero.
_PREDETERMINADOS = {
    "modelo_ollama": None,        # None = el de config.py
    "guardar_historial": True,
    "usar_auditor_gemini": True,
}
for clave, valor in _PREDETERMINADOS.items():
    st.session_state.setdefault(clave, valor)


paginas = st.navigation({
    "Investigación": [
        st.Page("paginas/investigacion.py", title="Nueva investigación", icon=":material/search:", default=True),
        st.Page("paginas/expedientes.py", title="Expedientes", icon=":material/folder_open:"),
    ],
    "Referencia": [
        st.Page("paginas/catalogo.py", title="Catálogo 69-B del SAT", icon=":material/gavel:"),
    ],
    "Sistema": [
        st.Page("paginas/configuracion.py", title="Configuración", icon=":material/settings:"),
    ],
})

paginas.run()
