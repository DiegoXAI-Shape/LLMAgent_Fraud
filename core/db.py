"""Inicialización y esquema de fraud.db (SQLite)."""

import sqlite3

from config import DB_PATH

# Catálogo de referencia: NUNCA se borra al ingerir un Excel nuevo. Es la
# lista pública del SAT (Definitivos + Presuntos, ~12,270 RFCs reales) --
# independiente de qué empresa se esté auditando ahora mismo. Si dependiera
# del ciclo de vida de `entities` (que sí se reemplaza con cada Excel nuevo),
# check_sat_blacklist() olvidaría todo el catálogo real cada vez que se
# cargara el dataset de otra empresa.
SCHEMA_REFERENCIA_SQL = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS sat_blacklist_69b (
    rfc VARCHAR(13) PRIMARY KEY,
    situacion TEXT CHECK(situacion IN ('PRESUNTO', 'DESVIRTUADO', 'DEFINITIVO')) NOT NULL,
    publicacion_dof DATE NOT NULL,
    monto_presunto_total DECIMAL(14,2) DEFAULT 0.0
);

CREATE INDEX IF NOT EXISTS idx_blacklist_situacion ON sat_blacklist_69b(situacion);

-- Historial de expedientes terminados. Vive en el esquema de REFERENCIA, no en
-- el del caso, y esa es toda la razón por la que existe: `investigation_cases`
-- se borra con cada ingesta nueva (está dentro de SCHEMA_CASO_SQL), así que
-- hasta ahora cada corrida destruía los dictámenes de la anterior. Aquí no.
--
-- Dos decisiones deliberadas:
--
-- 1. NO hay FK a `entities(rfc)`. Las entidades del caso desaparecen en la
--    siguiente corrida, así que una llave foránea volvería imposible conservar
--    el expediente de una empresa auditada la semana pasada. Es el mismo
--    motivo por el que `sat_blacklist_69b.rfc` tampoco la tiene.
-- 2. `payload_json` guarda los leads ya verificados (montos, identificadores,
--    evidencia) serializados. Con eso el expediente se puede volver a imprimir
--    en PDF meses después, cuando los datos del caso original ya no estén en
--    la base. Sin ese campo, el historial sería solo un recibo sin contenido.
CREATE TABLE IF NOT EXISTS expedientes_historial (
    expediente_id VARCHAR(36) PRIMARY KEY,
    fecha_hora TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    archivo_origen TEXT,
    n_confirmados INTEGER NOT NULL,
    n_descartados INTEGER NOT NULL,
    monto_total DECIMAL(14,2) NOT NULL DEFAULT 0.0,
    redactado_por TEXT CHECK(redactado_por IN ('gemini', 'plantilla')) NOT NULL,
    markdown TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_historial_fecha ON expedientes_historial(fecha_hora);
"""

# Datos del caso: la empresa que se está auditando AHORA MISMO. Esto SÍ se
# reemplaza por completo con cada Excel nuevo -- auditar una empresa distinta
# no debe mezclar sus facturas/pagos con los de una auditoría anterior
# (entre otras cosas, porque eso puede inventar ciclos de dinero falsos entre
# compañías que en la vida real nunca se relacionaron).
SCHEMA_CASO_SQL = """
PRAGMA foreign_keys = ON;

DROP VIEW IF EXISTS v_facturas_sin_pago_bancario;
DROP VIEW IF EXISTS v_money_network;
DROP TABLE IF EXISTS investigation_cases;
DROP TABLE IF EXISTS bank_ledger;
DROP TABLE IF EXISTS invoice_items;
DROP TABLE IF EXISTS invoices;
DROP TABLE IF EXISTS entities;

CREATE TABLE entities (
    rfc VARCHAR(13) PRIMARY KEY,
    razon_social TEXT NOT NULL,
    -- Nullable a propósito: un CFDI suelto (PDF/imagen) identifica al emisor y
    -- al receptor, pero NO dice cuándo se constituyó la empresa. Antes era NOT
    -- NULL, lo que obligaba a inventar una fecha para poder ingerir un
    -- documento — justo el tipo de dato fabricado que este sistema existe para
    -- evitar. Ningún detector lee este campo (se verificó con grep en todo el
    -- proyecto), así que dejarlo vacío no degrada ninguna detección.
    fecha_constitucion DATE,
    representante_legal TEXT,
    codigo_postal VARCHAR(5) NOT NULL,
    es_empresa_auditada BOOLEAN DEFAULT FALSE
);

CREATE TABLE invoices (
    uuid VARCHAR(36) PRIMARY KEY,
    emisor_rfc VARCHAR(13) NOT NULL REFERENCES entities(rfc),
    receptor_rfc VARCHAR(13) NOT NULL REFERENCES entities(rfc),
    fecha_emision TIMESTAMP NOT NULL,
    subtotal DECIMAL(14,2) NOT NULL,
    total DECIMAL(14,2) NOT NULL,
    metodo_pago VARCHAR(3) CHECK(metodo_pago IN ('PUE', 'PPD')) NOT NULL,
    forma_pago VARCHAR(2) NOT NULL,
    estado_cfdi TEXT CHECK(estado_cfdi IN ('VIGENTE', 'CANCELADO')) DEFAULT 'VIGENTE'
);

CREATE TABLE invoice_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_uuid VARCHAR(36) NOT NULL REFERENCES invoices(uuid),
    clave_prod_serv VARCHAR(8) NOT NULL,
    descripcion TEXT NOT NULL,
    cantidad DECIMAL(10,2) NOT NULL,
    valor_unitario DECIMAL(14,2) NOT NULL,
    importe DECIMAL(14,2) NOT NULL
);

CREATE TABLE bank_ledger (
    tx_id VARCHAR(36) PRIMARY KEY,
    cuenta_origen_rfc VARCHAR(13) NOT NULL REFERENCES entities(rfc),
    cuenta_destino_rfc VARCHAR(13) NOT NULL REFERENCES entities(rfc),
    fecha_hora TIMESTAMP NOT NULL,
    monto DECIMAL(14,2) NOT NULL,
    referencia_bancaria TEXT,
    cfdi_uuid VARCHAR(36) REFERENCES invoices(uuid)
);

CREATE TABLE investigation_cases (
    case_id VARCHAR(36) PRIMARY KEY,
    rfc_imputado VARCHAR(13) NOT NULL REFERENCES entities(rfc),
    tipo_esquema TEXT CHECK(tipo_esquema IN ('EFOS_69B', 'KICKBACK_CIRCULAR', 'EMPRESA_FACHADA', 'SIN_MATERIALIDAD', 'INGRESO_NO_DECLARADO')),
    monto_total_evidencia DECIMAL(14,2) NOT NULL,
    estatus_dictamen TEXT CHECK(estatus_dictamen IN ('CONFIRMADO_CON_PRUEBA', 'DESCARTADO_FALTA_EVIDENCIA')),
    justificacion_legal TEXT NOT NULL,
    regla_violada TEXT NOT NULL,
    fecha_dictamen TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_invoices_emisor ON invoices(emisor_rfc);
CREATE INDEX idx_invoices_receptor ON invoices(receptor_rfc);
CREATE INDEX idx_bank_origen ON bank_ledger(cuenta_origen_rfc);
CREATE INDEX idx_bank_destino ON bank_ledger(cuenta_destino_rfc);

CREATE VIEW v_money_network AS
SELECT cuenta_origen_rfc AS source, cuenta_destino_rfc AS target,
       SUM(monto) AS total_transferido, COUNT(tx_id) AS numero_operaciones
FROM bank_ledger
GROUP BY cuenta_origen_rfc, cuenta_destino_rfc;

CREATE VIEW v_facturas_sin_pago_bancario AS
SELECT i.uuid, i.emisor_rfc, i.receptor_rfc, i.total AS monto_facturado,
       COALESCE(SUM(b.monto), 0) AS monto_pagado,
       (i.total - COALESCE(SUM(b.monto), 0)) AS discrepancia
FROM invoices i
LEFT JOIN bank_ledger b ON i.uuid = b.cfdi_uuid
WHERE i.estado_cfdi = 'VIGENTE'
GROUP BY i.uuid;
"""


def get_connection(readonly: bool = False) -> sqlite3.Connection:
    if readonly:
        conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    else:
        conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    return conn


def init_db() -> None:
    """Crea el catálogo de referencia si no existe. Idempotente y seguro de
    llamar en cada arranque -- nunca borra `sat_blacklist_69b` si ya tiene
    datos."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_REFERENCIA_SQL)
        conn.commit()
    finally:
        conn.close()


def reset_case_data() -> None:
    """Reemplaza por completo los datos de la empresa en auditoría (entities,
    invoices, invoice_items, bank_ledger, investigation_cases). El catálogo
    de referencia del SAT (sat_blacklist_69b) NO se toca aquí."""
    conn = sqlite3.connect(DB_PATH)
    try:
        conn.executescript(SCHEMA_CASO_SQL)
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    init_db()
    reset_case_data()
    print(f"Base de datos inicializada en {DB_PATH}")
