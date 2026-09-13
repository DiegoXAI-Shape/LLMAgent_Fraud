"""Las 4 herramientas forenses deterministas que el Investigador puede invocar
vía function calling, más sus definiciones de JSON Schema."""

import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

import networkx as nx

from config import (
    DB_PATH,
    GIRO_CATALOG_PATH,
    MAX_RATIO_MONTO_CICLO,
    MIN_NODOS_CICLO,
    PERCENTIL_CICLO_MONTO,
    PERCENTIL_INGRESO_SIN_FACTURA,
    PISO_CICLO_MONTO,
    PISO_INGRESO_SIN_FACTURA,
)

_giro_catalog_cache: dict[str, dict] | None = None

_FORBIDDEN_SQL = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|ATTACH|DETACH|PRAGMA|REPLACE|VACUUM)\b",
    re.IGNORECASE,
)


class QueryNotAllowedError(ValueError):
    pass


def _readonly_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _load_giro_catalog() -> dict[str, dict]:
    global _giro_catalog_cache
    if _giro_catalog_cache is None:
        if GIRO_CATALOG_PATH.exists():
            _giro_catalog_cache = json.loads(GIRO_CATALOG_PATH.read_text(encoding="utf-8"))
        else:
            _giro_catalog_cache = {}
    return _giro_catalog_cache


def query_database(sql: str) -> list[dict]:
    """Ejecuta SOLO sentencias SELECT de solo lectura contra fraud.db."""
    cleaned = sql.strip().rstrip(";")

    if ";" in cleaned:
        raise QueryNotAllowedError("No se permiten múltiples statements separados por ';'.")
    if not re.match(r"^\s*SELECT\b", cleaned, re.IGNORECASE):
        raise QueryNotAllowedError("Solo se permiten sentencias SELECT.")
    if _FORBIDDEN_SQL.search(cleaned):
        raise QueryNotAllowedError("La query contiene palabras clave no permitidas.")

    conn = _readonly_connection()
    try:
        cursor = conn.execute(cleaned)
        rows = [dict(row) for row in cursor.fetchall()]
    finally:
        conn.close()
    return rows


def find_money_cycles(min_amount: float, max_hops: int,
                      max_ratio_monto: float = MAX_RATIO_MONTO_CICLO) -> list[dict]:
    """Busca ciclos de transferencias bancarias (round-tripping) sobre v_money_network.

    Un ciclo cualquiera NO es evidencia de nada: con suficientes empresas y
    transacciones, tarde o temprano el dinero da la vuelta por pura casualidad
    (comercio normal entre socios). Lo que distingue al round-tripping real es
    que cada salto regresa casi el mismo monto (menos una comisión pequeña) —
    esa es la firma de "lavar" dinero en un círculo. max_ratio_monto exige que
    el tramo más grande del ciclo no sea más de esa proporción del más chico;
    1.3 = los montos no difieren más de un 30% entre sí. Sin este filtro, un
    dataset con ~20 empresas y ~50 transacciones normales genera decenas de
    "ciclos" de puro ruido estadístico, no de fraude.
    """
    conn = _readonly_connection()
    try:
        rows = conn.execute("SELECT * FROM v_money_network").fetchall()
    finally:
        conn.close()

    graph = nx.DiGraph()
    for row in rows:
        graph.add_edge(
            row["source"], row["target"],
            total_transferido=row["total_transferido"],
            numero_operaciones=row["numero_operaciones"],
        )

    resultado = []
    for ciclo in nx.simple_cycles(graph, length_bound=max_hops):
        # Menos de 3 nodos NO es round-tripping: "A me paga y yo le pago" es
        # comercio bilateral normal. Ver MIN_NODOS_CICLO en config.py -- esta
        # línea exigía 2 y producía falsas acusaciones de lavado sobre pares de
        # empresas que simplemente se venden entre sí.
        if len(ciclo) < MIN_NODOS_CICLO:
            continue
        tramos = []
        for i in range(len(ciclo)):
            origen = ciclo[i]
            destino = ciclo[(i + 1) % len(ciclo)]
            monto_tramo = graph[origen][destino]["total_transferido"]
            tramos.append({"origen": origen, "destino": destino, "monto": monto_tramo})

        montos_tramo = [t["monto"] for t in tramos]
        monto_minimo_tramo = min(montos_tramo)
        ratio_monto = max(montos_tramo) / monto_minimo_tramo if monto_minimo_tramo > 0 else float("inf")

        if monto_minimo_tramo >= min_amount and ratio_monto <= max_ratio_monto:
            resultado.append({
                "ciclo_rfcs": ciclo,
                "num_saltos": len(ciclo),
                "monto_minimo_tramo": monto_minimo_tramo,
                "ratio_monto": round(ratio_monto, 3),
                "tramos": tramos,
            })

    return resultado


def umbral_monto_movimientos(percentil: float, piso: float) -> float:
    """Un umbral en pesos calibrado a ESTE dataset, no a pesos absolutos.

    POR QUÉ EXISTE: los umbrales del proyecto eran cifras fijas
    (`$50,000` para un ciclo de dinero, `$200,000` para un depósito sin
    factura). Estaban calibradas contra el dataset de demostración, donde los
    movimientos normales van de $8,000 a $450,000. Se midió qué pasa con los
    mismos fraudes a otra escala, sembrando la misma empresa tres veces con
    montos proporcionalmente menores:

        escala 1.00  (anillo $805,807, depósito $907,986)  ->  3 de 3 detectados
        escala 0.20  (anillo $168,702, depósito $215,597)  ->  3 de 3 detectados
        escala 0.05  (anillo  $34,468, depósito  $50,989)  ->  1 de 3 detectados

    A la escala chica solo sobrevivió el detector del listado 69-B, que es un
    JOIN y no compara montos. El anillo y el depósito quedaron por debajo del
    umbral y el triage nominó un único RFC — una pantalla casi vacía, idéntica
    a "aquí no hay fraude". Ese es el peor modo de falla posible en un sistema
    forense: un falso negativo que no se distingue de una revisión limpia.

    El problema de fondo es que "un monto grande" no significa lo mismo para un
    corporativo que para una PyME. Esta función responde a "grande PARA ESTA
    EMPRESA": toma el percentil indicado de los montos que realmente se mueven
    en `bank_ledger`, así que el mismo código se ajusta solo a los libros que
    le toquen.

    El `piso` es una red contra el caso degenerado: con poquísimos movimientos
    o montos minúsculos, un percentil puede caer tan bajo que todo pase el
    filtro y el triage nomine a media base. Se devuelve el mayor de los dos.
    """
    conn = _readonly_connection()
    try:
        montos = [
            float(fila[0])
            for fila in conn.execute("SELECT monto FROM bank_ledger WHERE monto IS NOT NULL")
        ]
    except sqlite3.Error:
        return piso
    finally:
        conn.close()

    if not montos:
        return piso

    montos.sort()
    # Percentil por posición, sin interpolar: basta para calibrar un filtro y
    # evita traer una dependencia solo para esto.
    indice = min(int(percentil * len(montos)), len(montos) - 1)
    return max(montos[indice], piso)


def umbral_ciclo_monto() -> float:
    """Umbral del tramo más chico de un ciclo, calibrado a este dataset."""
    return umbral_monto_movimientos(PERCENTIL_CICLO_MONTO, PISO_CICLO_MONTO)


def umbral_ingreso_sin_factura() -> float:
    """Umbral de un depósito sin CFDI, calibrado a este dataset.

    El triage (`investigator.get_candidate_rfcs`) y el verificador
    (`verifier._verify_ingreso_no_declarado`) llaman LOS DOS a esta función, y
    esa es la razón de que exista en vez de repetir el cálculo. Si el triage
    nominara con un umbral y el verificador exigiera otro, el sistema
    seleccionaría casos que después descarta él mismo — el mismo tipo de
    desincronización silenciosa que motivó centralizar los umbrales en
    `config.py`.
    """
    return umbral_monto_movimientos(PERCENTIL_INGRESO_SIN_FACTURA, PISO_INGRESO_SIN_FACTURA)


def check_sat_blacklist(rfc: str) -> dict:
    """Consulta la situación del RFC en la lista 69-B del SAT."""
    conn = _readonly_connection()
    try:
        row = conn.execute(
            "SELECT situacion, publicacion_dof, monto_presunto_total "
            "FROM sat_blacklist_69b WHERE rfc = ?",
            (rfc,),
        ).fetchone()
    finally:
        conn.close()

    if row is None:
        return {
            "rfc": rfc,
            "en_lista_69b": False,
            "situacion": None,
            "publicacion_dof": None,
            "monto_presunto_total": 0.0,
        }

    return {
        "rfc": rfc,
        "en_lista_69b": True,
        "situacion": row["situacion"],
        "publicacion_dof": row["publicacion_dof"],
        "monto_presunto_total": row["monto_presunto_total"],
    }


# "servicios" aparece en la descripción de TODOS los giros, así que coincidir en
# esa palabra no acredita nada: sin esta lista, cualquier concepto que empiece
# con "Servicios de..." se declara coherente con cualquier giro.
_STOPWORDS_GIRO = {"servicios", "servicio", "para", "otros", "general", "generales"}

# El riesgo de simulación (Art. 69-B CFF) no está en comprar fuera del giro
# propio — eso es comercio normal — sino en conceptos vagos, sin entregable
# verificable, que es justo como se disfrazan las operaciones inexistentes.
#
# OJO: "consultoria"/"asesoria" a secas NO van aquí. "Consultoría en sistemas
# de información" es una de nuestras propias descripciones legítimas de giro
# (CONSULTORIA_TI) — es un servicio concreto, no vago. Lo que sí delata falta
# de materialidad es cuando el concepto NO nombra ningún entregable concreto
# (ningún "sistemas", "software", "transporte", "publicidad"...) y en cambio
# usa una palabra "paraguas" que suena importante pero no dice nada: por eso
# "estrategica" sí está, pero "consultoria" sola no.
_TERMINOS_GENERICOS = {
    "estrategica", "profesionales diversos",
    "sin especificar", "apoyo administrativo", "gestion", "servicios diversos",
}


def _normalizar(texto: str) -> str:
    descompuesto = unicodedata.normalize("NFKD", texto.lower())
    return "".join(c for c in descompuesto if not unicodedata.combining(c))


def verify_service_materiality(rfc: str, invoice_uuid: str) -> dict:
    """Evalúa la materialidad de los conceptos REALES de una factura.

    El concepto se lee de la base de datos a partir del uuid -- NO lo
    proporciona quien llama. Antes esta función recibía el texto del concepto y
    el modelo lo inventaba: llamaba con "Servicios de consultoría en tecnología"
    cuando la factura de verdad decía "Consultoría estratégica en fusiones y
    adquisiciones", y como el inventado sí es específico, el fraude se declaraba
    inexistente. Mismo principio que el resto del sistema: el modelo señala QUÉ
    fila revisar, el código resuelve su contenido.
    """
    conn = _readonly_connection()
    try:
        filas = conn.execute(
            "SELECT descripcion FROM invoice_items WHERE invoice_uuid = ?", (invoice_uuid,)
        ).fetchall()
    finally:
        conn.close()

    if not filas:
        return {
            "rfc": rfc,
            "invoice_uuid": invoice_uuid,
            "match": None,
            "razon": f"No existe ninguna partida para la factura {invoice_uuid}.",
        }

    evaluaciones = [_evaluar_concepto(rfc, fila["descripcion"]) for fila in filas]
    # Basta con que UN concepto de la factura falle para marcarla.
    for evaluacion in evaluaciones:
        if evaluacion["match"] is False:
            return dict(evaluacion, invoice_uuid=invoice_uuid)
    return dict(evaluaciones[0], invoice_uuid=invoice_uuid)


def _descripciones_que_emite(rfc: str) -> list[str]:
    """Lo que la empresa VENDE, según sus propias facturas emitidas.

    El giro de un contribuyente está escrito en sus propios libros: si todas sus
    facturas como emisor dicen "servicios de transporte de carga", a eso se
    dedica. No hace falta un catálogo externo para saberlo.
    """
    conn = _readonly_connection()
    try:
        filas = conn.execute(
            "SELECT DISTINCT ii.descripcion FROM invoice_items ii "
            "JOIN invoices i ON i.uuid = ii.invoice_uuid "
            "WHERE i.emisor_rfc = ? LIMIT 50",
            (rfc,),
        ).fetchall()
    except sqlite3.Error:
        return []
    finally:
        conn.close()
    return [fila["descripcion"] for fila in filas if fila["descripcion"]]


def _razon_social_de(rfc: str) -> str | None:
    conn = _readonly_connection()
    try:
        fila = conn.execute("SELECT razon_social FROM entities WHERE rfc = ?", (rfc,)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return fila["razon_social"] if fila else None


def _evaluar_concepto(rfc: str, concepto: str) -> dict:
    """Compara un concepto contra el giro del RFC.

    Marca falta de materialidad (match=False) solo cuando se cumplen LAS DOS
    condiciones: el concepto no corresponde al giro Y además es un concepto
    genérico sin entregable verificable. Comprar un servicio específico fuera
    del giro propio es operación normal y no se marca.

    DE DÓNDE SALE EL GIRO, y por qué hay dos fuentes: antes solo se leía de
    `giro_catalog.json`, que genera nuestro propio generador de datos. Eso
    dejaba el esquema MUERTO en datos de un tercero — se midió: el triage sí
    nominaba a la empresa fachada de un dataset ajeno, pero la herramienta
    devolvía `match=None` ("el RFC no tiene giro registrado"), y como
    `_verify_materialidad` exige `False` para confirmar, el caso se descartaba
    SIEMPRE. Dos de los cinco esquemas gastaban una investigación completa del
    modelo para terminar en nada.

    El respaldo es más robusto y además más defendible ante un auditor: el giro
    se infiere de lo que la propia empresa FACTURA. Su historial dice a qué se
    dedica. Si no ha emitido nada, entonces sí es honesto decir que no se puede
    evaluar.
    """
    catalog = _load_giro_catalog()
    entry = catalog.get(rfc)

    if entry is not None:
        razon_social = entry["razon_social"]
        giro = entry["giro"]
        descripciones = entry["descripciones_esperadas"]
    else:
        descripciones = _descripciones_que_emite(rfc)
        if not descripciones:
            return {
                "rfc": rfc,
                "concepto": concepto,
                "match": None,
                "razon": (
                    f"{rfc} no tiene giro en el catálogo ni facturas emitidas en el "
                    "expediente, así que no hay con qué contrastar el concepto."
                ),
            }
        razon_social = _razon_social_de(rfc) or rfc
        giro = "inferido de su propio historial de facturación"

    concepto_norm = _normalizar(concepto)

    palabras_giro = {
        palabra
        for descripcion in descripciones
        for palabra in _normalizar(descripcion).split()
        if len(palabra) > 4 and palabra not in _STOPWORDS_GIRO
    }
    corresponde_al_giro = any(palabra in concepto_norm for palabra in palabras_giro)
    es_generico = any(termino in concepto_norm for termino in _TERMINOS_GENERICOS)

    match = corresponde_al_giro or not es_generico

    if corresponde_al_giro:
        razon = f"El concepto corresponde al giro ({giro})."
    elif not es_generico:
        razon = (
            f"El concepto ('{concepto}') es ajeno al giro de {razon_social} "
            f"({giro}), pero describe un servicio específico y verificable: "
            "no constituye por sí solo falta de materialidad."
        )
    else:
        muestra = ", ".join(sorted(set(descripciones))[:3])
        razon = (
            f"El concepto facturado ('{concepto}') es genérico y sin entregable verificable, "
            f"y además no corresponde al giro de {razon_social} ({giro}: {muestra})."
        )

    return {"rfc": rfc, "concepto": concepto, "match": match, "razon": razon}


TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": (
                "Ejecuta una consulta SELECT de solo lectura contra fraud.db "
                "(tablas: entities, sat_blacklist_69b, invoices, invoice_items, bank_ledger, "
                "investigation_cases; vistas: v_money_network, v_facturas_sin_pago_bancario)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "Sentencia SQL SELECT a ejecutar."},
                },
                "required": ["sql"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_money_cycles",
            "description": (
                "Busca ciclos de transferencias bancarias (round-tripping / kickback circular) "
                "sobre el grafo dirigido de v_money_network. Solo reporta ciclos donde los montos "
                "de cada tramo son similares entre sí (no difieren más de ~30%) — un ciclo con "
                "montos muy distintos entre tramos es comercio normal coincidental, no lavado."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "min_amount": {"type": "number", "description": "Monto mínimo del tramo más pequeño del ciclo."},
                    "max_hops": {"type": "integer", "description": "Número máximo de nodos (empresas) en el ciclo."},
                },
                "required": ["min_amount", "max_hops"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_sat_blacklist",
            "description": "Consulta si un RFC está en la lista del Artículo 69-B del CFF (EFOS).",
            "parameters": {
                "type": "object",
                "properties": {"rfc": {"type": "string", "description": "RFC a consultar."}},
                "required": ["rfc"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "verify_service_materiality",
            "description": (
                "Compara el concepto facturado contra el giro/objeto social registrado del RFC "
                "para detectar posible falta de materialidad del servicio."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "rfc": {"type": "string", "description": "RFC de la empresa a evaluar (normalmente el receptor de la factura)."},
                    "invoice_uuid": {"type": "string", "description": "uuid de la factura a revisar. La herramienta lee el concepto real de la base de datos; NO se lo pases tú."},
                },
                "required": ["rfc", "invoice_uuid"],
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "query_database": lambda args: query_database(args["sql"]),
    "find_money_cycles": lambda args: find_money_cycles(args["min_amount"], args["max_hops"]),
    "check_sat_blacklist": lambda args: check_sat_blacklist(args["rfc"]),
    "verify_service_materiality": lambda args: verify_service_materiality(args["rfc"], args["invoice_uuid"]),
}


def execute_tool_call(name: str, arguments: dict) -> Any:
    if name not in TOOL_FUNCTIONS:
        return {"error": f"Herramienta desconocida: {name}"}
    try:
        return TOOL_FUNCTIONS[name](arguments)
    except QueryNotAllowedError as exc:
        return {"error": str(exc)}
    except Exception as exc:
        return {"error": f"Error ejecutando {name}: {exc}"}
