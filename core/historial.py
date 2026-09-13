"""Historial de expedientes terminados, que sobrevive a las ingestas nuevas.

El problema que resuelve: `investigation_cases` vive dentro de `SCHEMA_CASO_SQL`
y por lo tanto se borra completa cada vez que se ingiere un archivo nuevo. Eso
es correcto para los datos del caso (mezclar las facturas de dos empresas
distintas inventaría ciclos de dinero que nunca existieron), pero convertía cada
corrida en la destrucción de la anterior: no había forma de volver a ver el
dictamen de ayer.

Aquí los expedientes se guardan en `expedientes_historial`, que vive en el
esquema de REFERENCIA -- el mismo que protege al catálogo del SAT y que nunca se
borra. Y se guardan con los leads verificados serializados dentro, de modo que
el PDF se puede reimprimir aunque los datos originales del caso ya no estén en
la base.
"""

import json
import uuid
from datetime import datetime
from typing import Any

from core.db import get_connection, init_db

ORIGENES_VALIDOS = {"gemini", "plantilla"}


def guardar_expediente(
    confirmados: list[dict[str, Any]],
    descartados: list[dict[str, Any]],
    markdown: str,
    redactado_por: str,
    archivo_origen: str | None = None,
) -> str:
    """Archiva un expediente terminado y devuelve su identificador.

    `descartados` se espera como lista de {"lead": dict, "razon": str}.
    """
    if redactado_por not in ORIGENES_VALIDOS:
        raise ValueError(f"redactado_por debe ser uno de {sorted(ORIGENES_VALIDOS)}")

    init_db()  # idempotente: asegura que la tabla de historial exista
    expediente_id = str(uuid.uuid4())
    monto_total = sum(float(l.get("monto_total_evidencia") or 0) for l in confirmados)

    # `default=str` protege contra tipos que json no sabe serializar (fechas,
    # Decimal) sin tirar el guardado: perder el historial por un tipo raro sería
    # peor que archivarlo con ese campo como texto.
    payload = json.dumps(
        {"confirmados": confirmados, "descartados": descartados},
        ensure_ascii=False, default=str,
    )

    conn = get_connection()
    try:
        conn.execute(
            "INSERT INTO expedientes_historial "
            "(expediente_id, fecha_hora, archivo_origen, n_confirmados, n_descartados, "
            " monto_total, redactado_por, markdown, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                expediente_id,
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                archivo_origen,
                len(confirmados),
                len(descartados),
                round(monto_total, 2),
                redactado_por,
                markdown,
                payload,
            ),
        )
        conn.commit()
    finally:
        conn.close()
    return expediente_id


def listar_expedientes(limite: int = 100) -> list[dict[str, Any]]:
    """Resúmenes de los expedientes archivados, del más reciente al más viejo.

    No devuelve `markdown` ni `payload_json`: la lista puede tener cientos de
    filas y esos campos pesan. Se cargan solo al abrir uno.
    """
    init_db()
    conn = get_connection(readonly=False)
    try:
        filas = conn.execute(
            "SELECT expediente_id, fecha_hora, archivo_origen, n_confirmados, "
            "       n_descartados, monto_total, redactado_por "
            "FROM expedientes_historial ORDER BY fecha_hora DESC LIMIT ?",
            (limite,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(fila) for fila in filas]


def obtener_expediente(expediente_id: str) -> dict[str, Any] | None:
    """Un expediente completo, con sus leads ya deserializados."""
    init_db()
    conn = get_connection(readonly=False)
    try:
        fila = conn.execute(
            "SELECT * FROM expedientes_historial WHERE expediente_id = ?", (expediente_id,)
        ).fetchone()
    finally:
        conn.close()

    if fila is None:
        return None

    expediente = dict(fila)
    try:
        payload = json.loads(expediente.get("payload_json") or "{}")
    except json.JSONDecodeError:
        payload = {}
    expediente["confirmados"] = payload.get("confirmados", [])
    expediente["descartados"] = payload.get("descartados", [])
    return expediente


def borrar_expediente(expediente_id: str) -> bool:
    init_db()
    conn = get_connection()
    try:
        cursor = conn.execute(
            "DELETE FROM expedientes_historial WHERE expediente_id = ?", (expediente_id,)
        )
        conn.commit()
        return cursor.rowcount > 0
    finally:
        conn.close()


def contar_expedientes() -> int:
    init_db()
    conn = get_connection(readonly=False)
    try:
        return int(conn.execute("SELECT COUNT(*) FROM expedientes_historial").fetchone()[0])
    finally:
        conn.close()
