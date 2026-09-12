"""Genera auditoria_empresa_input.xlsx: el Excel sintético que simula los
libros de una empresa para la demo. Inyecta 50 transacciones normales y 3
esquemas de fraude:

  a) Empresa listada en Art. 69-B del SAT (EFOS definitivo con facturación cobrada)
  b) Triangulación / kickback circular A->B->C->A por más de $500,000 MXN
  c) Empresa fachada sin materialidad (conceptos de consultoría ajenos al giro)

También escribe giro_catalog.json: catálogo auxiliar RFC -> giro/objeto social,
usado por tools.verify_service_materiality (la tabla entities no tiene columna
de giro, así que este catálogo vive fuera de la base de datos).
"""

import csv
import json
import random
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
from faker import Faker

from config import EXCEL_PATH, GIRO_CATALOG_PATH, SAT_69B_REFERENCE_PATH, SAT_CATALOGO_COMPLETO_PATH

# Injerto de identidad real: RFCs auténticos, DEFINITIVOS en el Artículo 69-B,
# descargados del listado oficial del SAT (omawww.sat.gob.mx/cifras_sat) y
# filtrados a publicación en el DOF >= 2024. Cada fila es (rfc, razon_social,
# fecha_real_de_publicacion_dof). El resto del expediente (facturas, pagos,
# víctima) se sintetiza alrededor de esa identidad real -- ver build_efos_scheme.

# Catálogo COMPLETO real (Definitivos + Presuntos, ~11,600 RFCs), independiente
# de qué empresa se use para el esquema sintético. Se carga entero a la hoja
# Lista_69B para que check_sat_blacklist() reconozca cualquier RFC real que
# aparezca en un dataset -- no solo el que nosotros elegimos para la demo.

random.seed(42)
Faker.seed(42)
fake = Faker("es_MX")

SECTORS = {
    "CONSTRUCCION": {
        "claves": ["72101505", "72141002"],
        "descripciones": [
            "Servicios de construcción de obra civil",
            "Mano de obra para remodelación de instalaciones",
        ],
    },
    "CONSULTORIA_TI": {
        "claves": ["81112501", "81111812"],
        "descripciones": [
            "Consultoría en sistemas de información",
            "Desarrollo de software a la medida",
        ],
    },
    "TRANSPORTE": {
        "claves": ["78101800", "78111808"],
        "descripciones": [
            "Servicios de transporte de carga terrestre",
            "Fletes y logística de distribución",
        ],
    },
    "LIMPIEZA": {
        "claves": ["76111501", "76111502"],
        "descripciones": [
            "Servicios de limpieza industrial",
            "Servicios de limpieza de oficinas",
        ],
    },
    "PUBLICIDAD": {
        "claves": ["82101500", "82101507"],
        "descripciones": [
            "Servicios de publicidad y marketing",
            "Diseño de campañas publicitarias",
        ],
    },
}
SECTOR_NAMES = list(SECTORS.keys())

CONCEPTO_SIN_MATERIALIDAD = "Consultoría estratégica en fusiones y adquisiciones corporativas"
CLAVE_SIN_MATERIALIDAD = "80101504"

DATE_CONSTITUCION_START = date(2012, 1, 1)
DATE_CONSTITUCION_END = date(2022, 12, 31)
DATE_INVOICE_START = date(2023, 7, 1)
DATE_INVOICE_END = date(2024, 12, 31)

entidades: list[dict] = []
lista_69b: list[dict] = []
facturas: list[dict] = []
partidas: list[dict] = []
transacciones: list[dict] = []
giro_catalog: dict[str, dict] = {}

used_rfcs: set[str] = set()


def gen_rfc(fecha_constitucion: date) -> str:
    while True:
        letras = "".join(random.choices("BCDFGHJKLMNPQRSTVWXYZ", k=3))
        fecha = fecha_constitucion.strftime("%y%m%d")
        homoclave = "".join(random.choices("0123456789ABCDEFGHIJKLMNPQRSTVWXYZ", k=3))
        rfc = f"{letras}{fecha}{homoclave}"
        if rfc not in used_rfcs:
            used_rfcs.add(rfc)
            return rfc


def random_date(start: date, end: date) -> date:
    return start + timedelta(days=random.randint(0, (end - start).days))


def to_ts(d: date) -> str:
    hh = random.randint(8, 19)
    mm = random.randint(0, 59)
    return datetime(d.year, d.month, d.day, hh, mm).strftime("%Y-%m-%d %H:%M:%S")


def add_entity(rfc: str, razon_social: str, fecha_constitucion: date, cp: str,
                sector: str | None = None, auditada: bool = False) -> None:
    entidades.append({
        "rfc": rfc,
        "razon_social": razon_social,
        "fecha_constitucion": fecha_constitucion.isoformat(),
        "representante_legal": fake.name(),
        "codigo_postal": cp,
        "es_empresa_auditada": bool(auditada),
    })
    if sector:
        giro_catalog[rfc] = {
            "razon_social": razon_social,
            "giro": sector,
            "claves_esperadas": SECTORS[sector]["claves"],
            "descripciones_esperadas": SECTORS[sector]["descripciones"],
        }


def add_invoice(emisor_rfc: str, receptor_rfc: str, fecha_emision: date, total: float,
                 sector: str | None, metodo_pago: str = "PUE", estado_cfdi: str = "VIGENTE",
                 concepto_override: str | None = None, clave_override: str | None = None) -> str:
    inv_uuid = str(uuid.uuid4())
    subtotal = round(total / 1.16, 2)
    facturas.append({
        "uuid": inv_uuid,
        "emisor_rfc": emisor_rfc,
        "receptor_rfc": receptor_rfc,
        "fecha_emision": to_ts(fecha_emision),
        "subtotal": subtotal,
        "total": total,
        "metodo_pago": metodo_pago,
        "forma_pago": random.choice(["01", "03", "99"]),
        "estado_cfdi": estado_cfdi,
    })
    descripcion = concepto_override or random.choice(SECTORS[sector]["descripciones"])
    clave = clave_override or random.choice(SECTORS[sector]["claves"])
    partidas.append({
        "invoice_uuid": inv_uuid,
        "clave_prod_serv": clave,
        "descripcion": descripcion,
        "cantidad": 1,
        "valor_unitario": subtotal,
        "importe": subtotal,
    })
    return inv_uuid


def add_bank_tx(origen_rfc: str, destino_rfc: str, fecha_hora: str, monto: float,
                 cfdi_uuid: str | None = None, referencia: str | None = None) -> str:
    tx_id = str(uuid.uuid4())
    transacciones.append({
        "tx_id": tx_id,
        "cuenta_origen_rfc": origen_rfc,
        "cuenta_destino_rfc": destino_rfc,
        "fecha_hora": fecha_hora,
        "monto": monto,
        "referencia_bancaria": referencia or f"SPEI-{random.randint(100000, 999999)}",
        "cfdi_uuid": cfdi_uuid,
    })
    return tx_id


def add_blacklist(rfc: str, situacion: str, publicacion_dof: date, monto_presunto_total: float) -> None:
    lista_69b.append({
        "rfc": rfc,
        "situacion": situacion,
        "publicacion_dof": publicacion_dof.isoformat(),
        "monto_presunto_total": monto_presunto_total,
    })


def build_normal_companies(n: int = 20) -> list[str]:
    rfcs = []
    for _ in range(n):
        fc = random_date(DATE_CONSTITUCION_START, DATE_CONSTITUCION_END)
        rfc = gen_rfc(fc)
        sector = random.choice(SECTOR_NAMES)
        add_entity(rfc, fake.company(), fc, fake.postcode()[:5], sector=sector)
        rfcs.append(rfc)
    return rfcs


def build_normal_transactions(normal_rfcs: list[str], n: int = 50) -> None:
    for _ in range(n):
        emisor, receptor = random.sample(normal_rfcs, 2)
        sector = giro_catalog[emisor]["giro"]
        fecha_emision = random_date(DATE_INVOICE_START, DATE_INVOICE_END)
        total = round(random.uniform(8_000, 450_000), 2)
        inv_uuid = add_invoice(emisor, receptor, fecha_emision, total, sector)
        fecha_pago = fecha_emision + timedelta(days=random.randint(0, 15))
        add_bank_tx(receptor, emisor, to_ts(fecha_pago), total, cfdi_uuid=inv_uuid)


def load_sat_69b_reales() -> list[tuple[str, str, date]]:
    with open(SAT_69B_REFERENCE_PATH, encoding="utf-8") as f:
        return [
            (row["rfc"].strip(), row["razon_social"].strip(), date.fromisoformat(row["publicacion_dof"]))
            for row in csv.DictReader(f)
        ]


def build_efos_scheme(normal_rfcs: list[str]) -> tuple[str, str]:
    efos_rfc, efos_razon_social, dof_date = random.choice(load_sat_69b_reales())
    used_rfcs.add(efos_rfc)

    # La fecha real de constitución de la EFOS no está en el listado público
    # del SAT (solo identidad + fechas de oficio) -- se sintetiza, acotada a
    # que sea muy anterior a la publicación real en el DOF.
    fc_efos = dof_date - timedelta(days=random.randint(365 * 3, 365 * 8))
    add_entity(efos_rfc, efos_razon_social, fc_efos, fake.postcode()[:5])

    fc_victima = random_date(DATE_CONSTITUCION_START, DATE_CONSTITUCION_END)
    victima_rfc = gen_rfc(fc_victima)
    sector = random.choice(SECTOR_NAMES)
    add_entity(victima_rfc, fake.company(), fc_victima, fake.postcode()[:5], sector=sector, auditada=True)

    # Las fechas de las facturas se derivan HACIA ATRÁS desde la fecha real de
    # publicación en el DOF -- la víctima ya se dedujo el gasto antes de que el
    # SAT publicara que el proveedor era una EFOS. Así opera el fraude real:
    # "by the time a supplier appears on the list, the company has already
    # claimed the deductions and is on the hook."
    invoice_dates = sorted(dof_date - timedelta(days=random.randint(90, 400)) for _ in range(2))
    for fecha_emision in invoice_dates:
        total = round(random.uniform(300_000, 2_500_000), 2)
        inv_uuid = add_invoice(
            efos_rfc, victima_rfc, fecha_emision, total, sector,
            concepto_override="Servicios profesionales diversos (sin especificar)",
            clave_override="80101504",
        )
        fecha_pago = fecha_emision + timedelta(days=random.randint(1, 5))
        add_bank_tx(victima_rfc, efos_rfc, to_ts(fecha_pago), total, cfdi_uuid=inv_uuid)

    # El listado público del SAT no publica montos presuntos, solo identidad y
    # fechas de oficio -- este campo sigue siendo sintético.
    add_blacklist(efos_rfc, "DEFINITIVO", dof_date, round(random.uniform(5_000_000, 40_000_000), 2))

    return efos_rfc, victima_rfc


def build_kickback_ring() -> list[str]:
    ring_rfcs = []
    for _ in range(3):
        fc = random_date(DATE_CONSTITUCION_START, DATE_CONSTITUCION_END)
        rfc = gen_rfc(fc)
        sector = random.choice(SECTOR_NAMES)
        add_entity(rfc, fake.company(), fc, fake.postcode()[:5], sector=sector, auditada=True)
        ring_rfcs.append(rfc)

    a, b, c = ring_rfcs
    base_monto = round(random.uniform(650_000, 900_000), 2)
    base_date = random_date(date(2024, 1, 1), date(2024, 9, 1))

    legs = [(a, b, base_monto), (b, c, round(base_monto * 0.97, 2)), (c, a, round(base_monto * 0.94, 2))]
    fecha = base_date
    for origen, destino, monto in legs:
        sector = giro_catalog[destino]["giro"]
        inv_uuid = add_invoice(destino, origen, fecha, monto, sector)
        fecha_pago = fecha + timedelta(days=random.randint(1, 3))
        add_bank_tx(origen, destino, to_ts(fecha_pago), monto, cfdi_uuid=inv_uuid)
        fecha = fecha_pago + timedelta(days=random.randint(2, 5))

    return ring_rfcs


def build_fachada_scheme(normal_rfcs: list[str]) -> str:
    fc = random_date(DATE_CONSTITUCION_START, DATE_CONSTITUCION_END)
    shell_rfc = gen_rfc(fc)
    add_entity(shell_rfc, fake.company(), fc, fake.postcode()[:5], sector="TRANSPORTE", auditada=True)

    emisor = random.choice(normal_rfcs)
    fecha_emision = random_date(DATE_INVOICE_START, DATE_INVOICE_END)
    total = round(random.uniform(400_000, 950_000), 2)
    inv_uuid = add_invoice(
        emisor, shell_rfc, fecha_emision, total, sector=None,
        concepto_override=CONCEPTO_SIN_MATERIALIDAD,
        clave_override=CLAVE_SIN_MATERIALIDAD,
    )
    fecha_pago = fecha_emision + timedelta(days=random.randint(1, 10))
    add_bank_tx(shell_rfc, emisor, to_ts(fecha_pago), total, cfdi_uuid=inv_uuid)

    return shell_rfc


def load_catalogo_completo_69b() -> list[tuple[str, str, str, str]]:
    with open(SAT_CATALOGO_COMPLETO_PATH, encoding="utf-8") as f:
        return [
            (row["rfc"].strip(), row["razon_social"].strip(), row["situacion"].strip(), row["publicacion_dof"].strip())
            for row in csv.DictReader(f)
        ]


def bulk_add_catalogo_completo_69b() -> int:
    ya_cargados = {fila["rfc"] for fila in lista_69b}
    agregados = 0
    for rfc, nombre, situacion, fecha in load_catalogo_completo_69b():
        if rfc in ya_cargados:
            continue
        lista_69b.append({
            "rfc": rfc,
            "situacion": situacion,
            "publicacion_dof": fecha,
            # El listado público del SAT no publica montos presuntos -- 0.0 es
            # "desconocido", no "cero pesos de fraude".
            "monto_presunto_total": 0.0,
        })
        ya_cargados.add(rfc)
        agregados += 1
    return agregados


def build_subdeclaracion_scheme(normal_rfcs: list[str]) -> str:
    """Caso trampa a propósito: dinero real que entra por banco pero nunca se
    facturó -- ingreso no declarado (Art. 59 CFF). Ninguno de los 4 detectores
    lo cubre (no está en 69-B, no hay factura de la que "no cuadre" el pago
    porque no hay factura en absoluto, no es un ciclo, no hay concepto que
    evaluar). Sirve para probar que el sistema calla ante un patrón que no
    sabe buscar, en vez de inventar una acusación."""
    fc = random_date(DATE_CONSTITUCION_START, DATE_CONSTITUCION_END)
    rfc = gen_rfc(fc)
    sector = random.choice(SECTOR_NAMES)
    add_entity(rfc, fake.company(), fc, fake.postcode()[:5], sector=sector)

    pagador = random.choice(normal_rfcs)
    fecha = random_date(DATE_INVOICE_START, DATE_INVOICE_END)
    monto = round(random.uniform(500_000, 1_500_000), 2)
    add_bank_tx(pagador, rfc, to_ts(fecha), monto, cfdi_uuid=None,
                referencia="Transferencia sin CFDI asociado")

    return rfc


def main() -> None:
    normal_rfcs = build_normal_companies(n=20)
    build_normal_transactions(normal_rfcs, n=50)
    efos_rfc, victima_rfc = build_efos_scheme(normal_rfcs)
    ring_rfcs = build_kickback_ring()
    shell_rfc = build_fachada_scheme(normal_rfcs)
    subdeclaracion_rfc = build_subdeclaracion_scheme(normal_rfcs)
    n_catalogo = bulk_add_catalogo_completo_69b()

    with pd.ExcelWriter(EXCEL_PATH, engine="openpyxl") as writer:
        pd.DataFrame(entidades).to_excel(writer, sheet_name="Entidades", index=False)
        pd.DataFrame(lista_69b).to_excel(writer, sheet_name="Lista_69B", index=False)
        pd.DataFrame(facturas).to_excel(writer, sheet_name="Facturas", index=False)
        pd.DataFrame(partidas).to_excel(writer, sheet_name="Partidas", index=False)
        pd.DataFrame(transacciones).to_excel(writer, sheet_name="Transacciones_Bancarias", index=False)

    GIRO_CATALOG_PATH.write_text(json.dumps(giro_catalog, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"OK -> {EXCEL_PATH}")
    print(f"  {len(normal_rfcs)} empresas normales, {len(facturas) - 5} facturas normales aprox.")
    print(f"  EFOS (69-B DEFINITIVO): {efos_rfc}")
    print(f"  Víctima EFOS (EFOS_69B): {victima_rfc}")
    print(f"  Anillo kickback (KICKBACK_CIRCULAR): {ring_rfcs}")
    print(f"  Empresa fachada (SIN_MATERIALIDAD): {shell_rfc}")
    print(f"  Caso trampa (subdeclaración, NO cubierto por ningún detector): {subdeclaracion_rfc}")
    print(f"  Catálogo completo real del SAT cargado: {n_catalogo} RFCs adicionales")
    print(f"Catálogo de giros -> {GIRO_CATALOG_PATH}")


if __name__ == "__main__":
    main()
