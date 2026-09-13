"""Rastro del dinero: convierte un caso verificado en un diagrama de flujo.

El brief pide dos cosas que una tabla no da: que *"el agente rastree el dinero en
pantalla"* y que el expediente tenga *"un rastro claro del dinero"*. Ver el dinero
salir de una empresa y regresar a ella cerrando un círculo comunica el fraude en
un segundo; la misma información en filas obliga al lector a reconstruir el
recorrido de cabeza.

DE DÓNDE SALEN LOS NODOS Y LAS ARISTAS: de la evidencia ya verificada del caso
(cada factura trae emisor y receptor, cada transferencia trae origen y destino,
resueltos por SQL en `verifier.enrich_lead`). No se dibuja nada que el
verificador no haya respaldado antes, igual que no se imprime ninguna cifra que
no venga de la base.

El diagrama se entrega como texto en formato DOT, que `st.graphviz_chart` dibuja
en el navegador. Esto es deliberado: el paquete de Python `graphviz` y la
herramienta `dot` del sistema NO se necesitan. La máquina donde se haga la
demostración puede no tenerlos instalados, y un diagrama que no aparece el día
de la presentación vale menos que no tenerlo.
"""

import sqlite3

from config import DB_PATH

# Mismos colores que el expediente en PDF y que el tema de la interfaz.
_AZUL = "#1F4E79"
_ROJO = "#A92D2D"
_GRIS = "#6E7A86"


def _escapar(texto: str) -> str:
    """DOT usa comillas dobles para delimitar etiquetas."""
    return str(texto).replace('"', '\\"')


def rfcs_en_lista_69b(rfcs: set[str]) -> set[str]:
    """Cuáles de esos RFC están en el listado 69-B con situación DEFINITIVO.

    Consulta `sat_blacklist_69b`, que vive en el esquema de referencia y NO se
    borra al ingerir un caso nuevo. Por eso esta función sigue siendo correcta
    incluso sobre un expediente archivado hace semanas, cuando los datos de ese
    caso ya no están en la base.
    """
    if not rfcs:
        return set()
    try:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    except sqlite3.Error:
        return set()
    try:
        marcadores = ",".join("?" for _ in rfcs)
        filas = conn.execute(
            f"SELECT rfc FROM sat_blacklist_69b WHERE situacion = 'DEFINITIVO' "
            f"AND rfc IN ({marcadores})",
            tuple(rfcs),
        ).fetchall()
        return {fila[0] for fila in filas}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _ciclo_del_caso(rfc_imputado: str) -> list[dict] | None:
    """Los tramos del ciclo de dinero que incluye al imputado, si existe.

    La evidencia citada de un kickback suele traer una o dos transferencias, no
    el anillo completo — y el anillo es justo lo que hay que ver. Se reconstruye
    desde `bank_ledger`. Si los datos de ese caso ya no están (expediente viejo),
    devuelve None y el diagrama se dibuja solo con la evidencia guardada.
    """
    try:
        from core import tools
        for ciclo in tools.find_money_cycles(min_amount=1.0, max_hops=8):
            if rfc_imputado in ciclo["ciclo_rfcs"]:
                return ciclo["tramos"]
    except Exception:
        return None
    return None


def construir_dot(lead: dict) -> str | None:
    """Diagrama DOT del flujo de dinero de un caso confirmado.

    Devuelve None si la evidencia no permite dibujar ningún movimiento — es
    preferible no mostrar nada a mostrar un diagrama vacío que sugiera que no
    hubo hallazgo.
    """
    imputado = str(lead.get("rfc_imputado") or "").strip()
    if not imputado:
        return None

    evidencia = [e for e in (lead.get("evidencia") or []) if e.get("existe")]

    # (origen, destino, etiqueta, es_ciclo)
    aristas: list[tuple[str, str, str, bool]] = []
    participantes: set[str] = {imputado}

    for item in evidencia:
        monto = item.get("monto")
        monto_txt = f"${monto:,.2f}" if isinstance(monto, (int, float)) else ""
        if item.get("tipo") == "factura":
            origen, destino = item.get("emisor_rfc"), item.get("receptor_rfc")
            etiqueta = f"CFDI  {monto_txt}"
        else:
            origen, destino = item.get("origen_rfc"), item.get("destino_rfc")
            etiqueta = f"transferencia  {monto_txt}"
        if origen and destino:
            aristas.append((str(origen), str(destino), etiqueta, False))
            participantes.update([str(origen), str(destino)])

    # Para un kickback, el anillo completo es la prueba visual; la evidencia
    # citada sola mostraría un tramo suelto que no se ve como fraude.
    if lead.get("tipo_esquema") == "KICKBACK_CIRCULAR":
        tramos = _ciclo_del_caso(imputado)
        for tramo in tramos or []:
            origen, destino = str(tramo["origen"]), str(tramo["destino"])
            monto = tramo.get("monto")
            etiqueta = f"${monto:,.2f}" if isinstance(monto, (int, float)) else ""
            if not any(o == origen and d == destino for o, d, _, _ in aristas):
                aristas.append((origen, destino, etiqueta, True))
            participantes.update([origen, destino])

    if not aristas:
        return None

    listados = rfcs_en_lista_69b(participantes)
    razon_social = str(lead.get("razon_social") or "")

    lineas = [
        "digraph rastro {",
        '  rankdir=LR; bgcolor="transparent";',
        '  node [shape=box style="rounded,filled" fontname="Helvetica" fontsize=10 '
        'penwidth=0 margin="0.18,0.10"];',
        '  edge [fontname="Helvetica" fontsize=9 color="#94A3B8" penwidth=1.3];',
    ]

    for rfc in sorted(participantes):
        if rfc == imputado:
            etiqueta = f"{rfc}\\n{razon_social}" if razon_social else rfc
            lineas.append(
                f'  "{_escapar(rfc)}" [label="{_escapar(etiqueta)}" '
                f'fillcolor="{_ROJO}" fontcolor="white"];'
            )
        elif rfc in listados:
            lineas.append(
                f'  "{_escapar(rfc)}" [label="{_escapar(rfc)}\\n69-B DEFINITIVO" '
                f'fillcolor="#7A1F1F" fontcolor="white"];'
            )
        else:
            lineas.append(
                f'  "{_escapar(rfc)}" [label="{_escapar(rfc)}" '
                f'fillcolor="#E8EEF5" fontcolor="{_AZUL}"];'
            )

    for origen, destino, etiqueta, es_ciclo in aristas:
        estilo = f'color="{_ROJO}" penwidth=2.0' if es_ciclo else f'color="{_GRIS}"'
        lineas.append(
            f'  "{_escapar(origen)}" -> "{_escapar(destino)}" '
            f'[label="  {_escapar(etiqueta)}" {estilo}];'
        )

    lineas.append("}")
    return "\n".join(lineas)


def leyenda(lead: dict) -> str:
    """Una línea que explica qué se está viendo, según la tipología."""
    return {
        "KICKBACK_CIRCULAR": "En rojo, el ciclo cerrado: el dinero sale y regresa al "
                             "mismo RFC. Ese retorno es lo que distingue el lavado del comercio normal.",
        "EFOS_69B": "En rojo oscuro, el proveedor con situación DEFINITIVO en el listado "
                    "69-B del SAT. Las facturas que le compró son deducciones sobre operaciones simuladas.",
        "INGRESO_NO_DECLARADO": "Depósitos recibidos sin un CFDI que los ampare.",
        "SIN_MATERIALIDAD": "Facturas cuyo concepto no corresponde a un entregable verificable.",
        "EMPRESA_FACHADA": "Operaciones sin sustancia económica detrás.",
    }.get(str(lead.get("tipo_esquema")), "Flujo de dinero acreditado en el expediente.")
