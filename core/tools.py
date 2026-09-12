"""Las 4 herramientas forenses deterministas que el Investigador puede invocar
vía function calling, más sus definiciones de JSON Schema."""

import json
import re
import sqlite3
import unicodedata
from pathlib import Path
from typing import Any

import networkx as nx

from config import DB_PATH, GIRO_CATALOG_PATH, MAX_RATIO_MONTO_CICLO

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
        if len(ciclo) < 2:
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


def verify_service_materiality(rfc: str, concepto: str) -> dict:
    """Evalúa la materialidad del concepto facturado frente al giro del RFC.

    Marca falta de materialidad (match=False) solo cuando se cumplen LAS DOS
    condiciones: el concepto no corresponde al giro registrado Y además es un
    concepto genérico sin entregable verificable. Comprar un servicio específico
    fuera del giro propio es operación normal y no se marca.
    """
    catalog = _load_giro_catalog()
    entry = catalog.get(rfc)

    if entry is None:
        return {
            "rfc": rfc,
            "concepto": concepto,
            "match": None,
            "razon": "El RFC no tiene un giro registrado en el catálogo; no se puede evaluar materialidad.",
        }

    concepto_norm = _normalizar(concepto)

    palabras_giro = {
        palabra
        for descripcion in entry["descripciones_esperadas"]
        for palabra in _normalizar(descripcion).split()
        if len(palabra) > 4 and palabra not in _STOPWORDS_GIRO
    }
    corresponde_al_giro = any(palabra in concepto_norm for palabra in palabras_giro)
    es_generico = any(termino in concepto_norm for termino in _TERMINOS_GENERICOS)

    match = corresponde_al_giro or not es_generico

    if corresponde_al_giro:
        razon = f"El concepto corresponde al giro registrado ({entry['giro']})."
    elif not es_generico:
        razon = (
            f"El concepto ('{concepto}') es ajeno al giro de {entry['razon_social']} "
            f"({entry['giro']}), pero describe un servicio específico y verificable: "
            "no constituye por sí solo falta de materialidad."
        )
    else:
        razon = (
            f"El concepto facturado ('{concepto}') es genérico y sin entregable verificable, "
            f"y además no corresponde al giro registrado de {entry['razon_social']} "
            f"({entry['giro']}: {', '.join(entry['descripciones_esperadas'])})."
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
                    "rfc": {"type": "string", "description": "RFC de la empresa a evaluar (normalmente el receptor)."},
                    "concepto": {"type": "string", "description": "Descripción del concepto facturado."},
                },
                "required": ["rfc", "concepto"],
            },
        },
    },
]

TOOL_FUNCTIONS = {
    "query_database": lambda args: query_database(args["sql"]),
    "find_money_cycles": lambda args: find_money_cycles(args["min_amount"], args["max_hops"]),
    "check_sat_blacklist": lambda args: check_sat_blacklist(args["rfc"]),
    "verify_service_materiality": lambda args: verify_service_materiality(args["rfc"], args["concepto"]),
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
