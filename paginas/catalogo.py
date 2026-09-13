"""Catálogo 69-B del SAT y validador de RFC.

Dos cosas que conviene poder enseñar en vivo:

1. Que la lista negra NO es inventada: son los RFC reales publicados por el SAT.
2. Que un RFC mal leído (por OCR, por visión, por dedazo) se detecta con código,
   no con confianza en el modelo. El dígito verificador del propio RFC delata
   el error, y si aun así pasa, la búsqueda por cercanía lo caza contra el
   universo conocido.
"""

import pandas as pd
import streamlit as st

from core.db import get_connection, init_db
from core.rfc import digito_verificador, revisar_rfc_leido

st.title("Catálogo 69-B del SAT")

init_db()


@st.cache_data(show_spinner=False)
def _cargar_catalogo() -> pd.DataFrame:
    conn = get_connection(readonly=True)
    try:
        return pd.read_sql_query(
            "SELECT rfc, situacion, publicacion_dof, monto_presunto_total "
            "FROM sat_blacklist_69b ORDER BY publicacion_dof DESC", conn
        )
    finally:
        conn.close()


try:
    catalogo = _cargar_catalogo()
except Exception as exc:
    st.error(f"No se pudo leer el catálogo: {exc}")
    st.stop()

if catalogo.empty:
    st.warning(
        "El catálogo está vacío. Se llena al ingerir un caso o corriendo "
        "`python -m data_pipeline.sat_downloader`."
    )
    st.stop()

conteos = catalogo["situacion"].value_counts().to_dict()
c1, c2, c3, c4 = st.columns(4)
c1.metric("RFC en el catálogo", f"{len(catalogo):,}")
c2.metric("Definitivos", f"{conteos.get('DEFINITIVO', 0):,}")
c3.metric("Presuntos", f"{conteos.get('PRESUNTO', 0):,}")
c4.metric("Desvirtuados", f"{conteos.get('DESVIRTUADO', 0):,}")

st.caption(
    "Listado del Artículo 69-B del CFF publicado por el SAT. Un RFC en situación "
    "**DEFINITIVO** es el único que el verificador acepta como prueba de EFOS: "
    "un *presunto* todavía puede desvirtuarse, y acusar sobre esa base sería "
    "imputar algo que no se puede sostener."
)

st.divider()

tab_buscar, tab_validar = st.tabs(["Buscar en el catálogo", "Validar un RFC leído"])

with tab_buscar:
    izquierda, derecha = st.columns([2, 1])
    consulta = izquierda.text_input("Buscar por RFC", placeholder="p. ej. AAA120730")
    situaciones = derecha.multiselect(
        "Situación", options=sorted(catalogo["situacion"].unique()),
        default=["DEFINITIVO"],
    )

    filtrado = catalogo
    if situaciones:
        filtrado = filtrado[filtrado["situacion"].isin(situaciones)]
    if consulta.strip():
        filtrado = filtrado[filtrado["rfc"].str.contains(consulta.strip().upper(), na=False)]

    st.write(f"**{len(filtrado):,}** resultado(s)")
    st.dataframe(filtrado.head(500), hide_index=True, width="stretch")
    if len(filtrado) > 500:
        st.caption("Se muestran los primeros 500 resultados.")

with tab_validar:
    st.write(
        "Cuando un RFC entra por foto o PDF escaneado, el modelo puede leerlo mal. "
        "Un RFC equivocado no produce un error: produce un *'no está en la lista'* "
        "idéntico a una revisión limpia. Esto lo detecta antes de que eso pase."
    )
    rfc_prueba = st.text_input(
        "RFC a validar", placeholder="ADSR51130N5A",
        help="El último carácter de un RFC es un dígito verificador calculado a partir de los otros once.",
    )

    if rfc_prueba.strip():
        dictamen = revisar_rfc_leido(rfc_prueba.strip().upper())
        if dictamen["confiable"]:
            st.success(dictamen["motivo"], icon="✅")
        else:
            st.error(dictamen["motivo"], icon="🚨")

        col1, col2, col3 = st.columns(3)
        checksum = dictamen["checksum_ok"]
        col1.metric("Dígito verificador",
                    "correcto" if checksum else ("no evaluable" if checksum is None else "incorrecto"))
        col2.metric("Dígito esperado", digito_verificador(rfc_prueba.strip().upper()) or "—")
        col3.metric("En la base", "sí" if dictamen["conocido"] else "no")

        if dictamen["sugerencias"]:
            st.info("RFC conocidos parecidos: " + ", ".join(f"`{s}`" for s in dictamen["sugerencias"][:5]))
