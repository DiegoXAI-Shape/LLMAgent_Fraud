"""CLI: toma auditoria_empresa_input.xlsx, valida cada hoja y la vuelca a
fraud.db. El Excel deja de existir para el resto del pipeline a partir de aquí."""

import argparse
import sqlite3
import sys
from pathlib import Path

import pandas as pd

from config import EXCEL_PATH as DEFAULT_EXCEL_PATH
from core.db import get_connection, init_db, reset_case_data

REQUIRED_SHEETS = ["Entidades", "Lista_69B", "Facturas", "Partidas", "Transacciones_Bancarias"]

ENTITIES_COLUMNS = ["rfc", "razon_social", "fecha_constitucion", "representante_legal",
                     "codigo_postal", "es_empresa_auditada"]
BLACKLIST_COLUMNS = ["rfc", "situacion", "publicacion_dof", "monto_presunto_total"]
INVOICES_COLUMNS = ["uuid", "emisor_rfc", "receptor_rfc", "fecha_emision", "subtotal",
                     "total", "metodo_pago", "forma_pago", "estado_cfdi"]
ITEMS_COLUMNS = ["invoice_uuid", "clave_prod_serv", "descripcion", "cantidad",
                  "valor_unitario", "importe"]
LEDGER_COLUMNS = ["tx_id", "cuenta_origen_rfc", "cuenta_destino_rfc", "fecha_hora",
                   "monto", "referencia_bancaria", "cfdi_uuid"]

VALID_SITUACIONES = {"PRESUNTO", "DESVIRTUADO", "DEFINITIVO"}
VALID_METODOS_PAGO = {"PUE", "PPD"}
VALID_ESTADOS_CFDI = {"VIGENTE", "CANCELADO"}


class IngestValidationError(ValueError):
    pass


def _require_columns(df: pd.DataFrame, sheet_name: str, columns: list[str]) -> None:
    faltantes = [c for c in columns if c not in df.columns]
    if faltantes:
        raise IngestValidationError(f"Hoja '{sheet_name}': faltan columnas {faltantes}")


def _require_non_null(df: pd.DataFrame, sheet_name: str, columns: list[str]) -> None:
    for col in columns:
        nulos = df[df[col].isna()]
        if not nulos.empty:
            filas = list(nulos.index + 2)
            raise IngestValidationError(f"Hoja '{sheet_name}', columna '{col}': valores nulos en filas {filas}")


def _parse_date(value, sheet_name: str, columna: str, fila: int) -> str:
    try:
        ts = pd.to_datetime(value)
    except Exception as exc:
        raise IngestValidationError(
            f"Hoja '{sheet_name}', columna '{columna}', fila {fila + 2}: fecha inválida '{value}' ({exc})"
        )
    return ts.strftime("%Y-%m-%d")


def _parse_timestamp(value, sheet_name: str, columna: str, fila: int) -> str:
    try:
        ts = pd.to_datetime(value)
    except Exception as exc:
        raise IngestValidationError(
            f"Hoja '{sheet_name}', columna '{columna}', fila {fila + 2}: fecha/hora inválida '{value}' ({exc})"
        )
    return ts.strftime("%Y-%m-%d %H:%M:%S")


def _parse_float(value, sheet_name: str, columna: str, fila: int) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise IngestValidationError(
            f"Hoja '{sheet_name}', columna '{columna}', fila {fila + 2}: valor numérico inválido '{value}'"
        )


def _parse_bool(value) -> int:
    # NaN primero: `bool(float('nan'))` es True en Python (cualquier float
    # distinto de 0.0 lo es), así que sin este chequeo una celda VACÍA se leía
    # como "sí es empresa auditada". Esto importaba de verdad porque
    # universal_loader ahora rellena con NaN las columnas canónicas que ninguna
    # hoja ajena trae -- sin este fix, un Excel de un tercero marcaría a TODAS
    # las empresas como auditadas.
    if pd.isna(value):
        return 0
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return int(bool(value))
    texto = str(value).strip().upper()
    return 1 if texto in {"TRUE", "1", "SI", "SÍ", "YES"} else 0


def load_workbook(excel_path: Path) -> dict[str, pd.DataFrame]:
    hojas = pd.read_excel(excel_path, sheet_name=None)
    faltantes = [s for s in REQUIRED_SHEETS if s not in hojas]
    if faltantes:
        raise IngestValidationError(f"Faltan hojas requeridas en el Excel: {faltantes}")
    return hojas


def build_entities_rows(df: pd.DataFrame) -> list[tuple]:
    _require_columns(df, "Entidades", ENTITIES_COLUMNS)
    # fecha_constitucion NO se exige: un CFDI suelto no la contiene y la
    # alternativa sería inventarla. Ver la nota en el DDL de core/db.py.
    _require_non_null(df, "Entidades", ["rfc", "razon_social", "codigo_postal"])
    rows = []
    for i, row in df.iterrows():
        rows.append((
            str(row["rfc"]).strip(),
            str(row["razon_social"]).strip(),
            None if pd.isna(row.get("fecha_constitucion"))
            else _parse_date(row["fecha_constitucion"], "Entidades", "fecha_constitucion", i),
            None if pd.isna(row.get("representante_legal")) else str(row["representante_legal"]).strip(),
            str(row["codigo_postal"]).strip().zfill(5)[:5],
            _parse_bool(row.get("es_empresa_auditada", False)),
        ))
    return rows


def build_blacklist_rows(df: pd.DataFrame) -> list[tuple]:
    if df.empty:
        return []
    _require_columns(df, "Lista_69B", BLACKLIST_COLUMNS)
    _require_non_null(df, "Lista_69B", ["rfc", "situacion", "publicacion_dof"])
    rows = []
    for i, row in df.iterrows():
        situacion = str(row["situacion"]).strip().upper()
        if situacion not in VALID_SITUACIONES:
            raise IngestValidationError(f"Hoja 'Lista_69B', fila {i + 2}: situacion inválida '{situacion}'")
        rows.append((
            str(row["rfc"]).strip(),
            situacion,
            _parse_date(row["publicacion_dof"], "Lista_69B", "publicacion_dof", i),
            # Mismo cuidado que con es_empresa_auditada: `.get(clave, 0.0)` NO
            # usa el default si la columna EXISTE pero viene vacía (NaN) -- solo
            # cuando la clave falta por completo. Con la columna rellenada por
            # universal_loader, `float(nan)` pasaba sin error y guardaba NaN en
            # la base en vez del 0.0 que este campo espera como "desconocido".
            0.0 if pd.isna(row.get("monto_presunto_total"))
            else _parse_float(row["monto_presunto_total"], "Lista_69B", "monto_presunto_total", i),
        ))
    return rows


def build_invoices_rows(df: pd.DataFrame) -> list[tuple]:
    _require_columns(df, "Facturas", INVOICES_COLUMNS)
    _require_non_null(df, "Facturas", ["uuid", "emisor_rfc", "receptor_rfc", "fecha_emision", "total"])
    rows = []
    for i, row in df.iterrows():
        metodo_pago = str(row["metodo_pago"]).strip().upper()
        if metodo_pago not in VALID_METODOS_PAGO:
            raise IngestValidationError(f"Hoja 'Facturas', fila {i + 2}: metodo_pago inválido '{metodo_pago}'")
        estado_cfdi = str(row.get("estado_cfdi", "VIGENTE")).strip().upper()
        if estado_cfdi not in VALID_ESTADOS_CFDI:
            estado_cfdi = "VIGENTE"
        rows.append((
            str(row["uuid"]).strip(),
            str(row["emisor_rfc"]).strip(),
            str(row["receptor_rfc"]).strip(),
            _parse_timestamp(row["fecha_emision"], "Facturas", "fecha_emision", i),
            _parse_float(row["subtotal"], "Facturas", "subtotal", i),
            _parse_float(row["total"], "Facturas", "total", i),
            metodo_pago,
            str(row["forma_pago"]).strip(),
            estado_cfdi,
        ))
    return rows


def build_items_rows(df: pd.DataFrame) -> list[tuple]:
    _require_columns(df, "Partidas", ITEMS_COLUMNS)
    _require_non_null(df, "Partidas", ["invoice_uuid", "clave_prod_serv", "descripcion", "importe"])
    rows = []
    for i, row in df.iterrows():
        rows.append((
            str(row["invoice_uuid"]).strip(),
            str(row["clave_prod_serv"]).strip(),
            str(row["descripcion"]).strip(),
            _parse_float(row["cantidad"], "Partidas", "cantidad", i),
            _parse_float(row["valor_unitario"], "Partidas", "valor_unitario", i),
            _parse_float(row["importe"], "Partidas", "importe", i),
        ))
    return rows


def build_ledger_rows(df: pd.DataFrame) -> list[tuple]:
    _require_columns(df, "Transacciones_Bancarias", LEDGER_COLUMNS)
    _require_non_null(df, "Transacciones_Bancarias",
                       ["tx_id", "cuenta_origen_rfc", "cuenta_destino_rfc", "fecha_hora", "monto"])
    rows = []
    for i, row in df.iterrows():
        cfdi_uuid = row.get("cfdi_uuid")
        rows.append((
            str(row["tx_id"]).strip(),
            str(row["cuenta_origen_rfc"]).strip(),
            str(row["cuenta_destino_rfc"]).strip(),
            _parse_timestamp(row["fecha_hora"], "Transacciones_Bancarias", "fecha_hora", i),
            _parse_float(row["monto"], "Transacciones_Bancarias", "monto", i),
            None if pd.isna(row.get("referencia_bancaria")) else str(row["referencia_bancaria"]).strip(),
            None if pd.isna(cfdi_uuid) else str(cfdi_uuid).strip(),
        ))
    return rows


def run_ingest(excel_path: Path = DEFAULT_EXCEL_PATH) -> dict[str, int]:
    excel_path = Path(excel_path)
    if not excel_path.exists():
        raise FileNotFoundError(f"No se encontró el archivo: {excel_path}")

    hojas = load_workbook(excel_path)

    entities_rows = build_entities_rows(hojas["Entidades"])
    blacklist_rows = build_blacklist_rows(hojas["Lista_69B"])
    invoices_rows = build_invoices_rows(hojas["Facturas"])
    items_rows = build_items_rows(hojas["Partidas"])
    ledger_rows = build_ledger_rows(hojas["Transacciones_Bancarias"])

    init_db()
    reset_case_data()
    conn = get_connection()
    try:
        conn.executemany(
            "INSERT OR REPLACE INTO entities "
            "(rfc, razon_social, fecha_constitucion, representante_legal, codigo_postal, es_empresa_auditada) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            entities_rows,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO sat_blacklist_69b "
            "(rfc, situacion, publicacion_dof, monto_presunto_total) VALUES (?, ?, ?, ?)",
            blacklist_rows,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO invoices "
            "(uuid, emisor_rfc, receptor_rfc, fecha_emision, subtotal, total, metodo_pago, forma_pago, estado_cfdi) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            invoices_rows,
        )
        conn.executemany(
            "INSERT INTO invoice_items "
            "(invoice_uuid, clave_prod_serv, descripcion, cantidad, valor_unitario, importe) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            items_rows,
        )
        conn.executemany(
            "INSERT OR REPLACE INTO bank_ledger "
            "(tx_id, cuenta_origen_rfc, cuenta_destino_rfc, fecha_hora, monto, referencia_bancaria, cfdi_uuid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ledger_rows,
        )
        conn.commit()
    except sqlite3.IntegrityError as exc:
        conn.rollback()
        raise IngestValidationError(f"Violación de integridad referencial al insertar: {exc}")
    finally:
        conn.close()

    return {
        "entities": len(entities_rows),
        "sat_blacklist_69b": len(blacklist_rows),
        "invoices": len(invoices_rows),
        "invoice_items": len(items_rows),
        "bank_ledger": len(ledger_rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Ingesta el Excel de la empresa a fraud.db")
    parser.add_argument("excel_path", nargs="?", default=str(DEFAULT_EXCEL_PATH))
    args = parser.parse_args()

    try:
        counts = run_ingest(Path(args.excel_path))
    except (IngestValidationError, FileNotFoundError) as exc:
        print(f"ERROR de ingesta: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Ingesta completada:")
    for tabla, n in counts.items():
        print(f"  {tabla}: {n} filas")


if __name__ == "__main__":
    main()
