"""Filtro determinista (Fase 2) — sin LLM.

Dos responsabilidades:

1. `enrich_lead`: resuelve contra fraud.db todo lo que el modelo NO debe generar.
   El Investigador solo entrega IDENTIFICADORES (uuid de factura / tx_id bancario);
   los montos exactos y la razón social se leen con SQL. Ninguna cifra del
   expediente se genera token por token con el LLM.

2. `verify_lead`: deja pasar solo los leads con prueba irrefutable — cada fila
   citada existe físicamente, y la tipología tiene su vínculo probatorio
   correspondiente (69-B DEFINITIVO, ciclo de dinero cerrado, o inconsistencia
   de materialidad verificada).
"""

from typing import Any

from core import tools
from core.db import get_connection

ESQUEMAS_VALIDOS = {"EFOS_69B", "KICKBACK_CIRCULAR", "EMPRESA_FACHADA", "SIN_MATERIALIDAD", "INGRESO_NO_DECLARADO"}


CONFIRMADO = "CONFIRMADO_CON_PRUEBA"
DESCARTADO = "DESCARTADO_FALTA_EVIDENCIA"


def _get_invoice(uuid: str) -> dict | None:
    conn = get_connection(readonly=True)
    try:
        row = conn.execute(
            "SELECT uuid, emisor_rfc, receptor_rfc, total FROM invoices WHERE uuid = ?", (uuid,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _get_invoice_items(invoice_uuid: str) -> list[dict]:
    conn = get_connection(readonly=True)
    try:
        rows = conn.execute(
            "SELECT descripcion FROM invoice_items WHERE invoice_uuid = ?", (invoice_uuid,)
        ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def _get_bank_tx(tx_id: str) -> dict | None:
    conn = get_connection(readonly=True)
    try:
        row = conn.execute(
            "SELECT tx_id, cuenta_origen_rfc, cuenta_destino_rfc, monto, cfdi_uuid "
            "FROM bank_ledger WHERE tx_id = ?", (tx_id,)
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _get_razon_social(rfc: str) -> str | None:
    conn = get_connection(readonly=True)
    try:
        row = conn.execute("SELECT razon_social FROM entities WHERE rfc = ?", (rfc,)).fetchone()
    finally:
        conn.close()
    return row["razon_social"] if row else None


def _entity_exists(rfc: str) -> bool:
    return _get_razon_social(rfc) is not None


def enrich_lead(lead_dict: dict) -> dict:
    """Resuelve montos y razón social desde fraud.db. Idempotente."""
    enriquecido = dict(lead_dict)
    rfc = str(enriquecido.get("rfc_imputado", ""))
    enriquecido["razon_social"] = _get_razon_social(rfc) or ""

    resueltos: list[dict[str, Any]] = []
    for item in lead_dict.get("evidencia") or []:
        # El modelo a veces rellena la lista con un hueco vacío para que se
        # parezca al ejemplo del prompt: {"tipo": "transferencia", "tx_id": null}.
        # Un elemento sin identificador no aporta ni invalida nada -- se ignora,
        # en vez de tumbar un caso que sí tiene evidencia real en los demás
        # elementos. Lo que queda sigue pasando por toda la verificación.
        identificador = item.get("uuid") or item.get("tx_id")
        if identificador is None or not str(identificador).strip():
            continue

        # El identificador manda sobre la etiqueta: el modelo a veces omite "tipo",
        # pero un uuid solo puede ser factura y un tx_id solo puede ser transferencia.
        tipo = item.get("tipo")
        if tipo not in {"factura", "transferencia"}:
            tipo = "factura" if item.get("uuid") else "transferencia" if item.get("tx_id") else tipo

        if tipo == "factura":
            fila = _get_invoice(str(item.get("uuid", "")))
            resueltos.append({
                "tipo": "factura",
                "uuid": item.get("uuid"),
                "monto": float(fila["total"]) if fila else None,
                "existe": fila is not None,
                # Que la fila exista no basta: tiene que involucrar al acusado. Si no,
                # se pueden colgar facturas ajenas para inflar el monto defraudado.
                "relevante": bool(fila and rfc in (fila["emisor_rfc"], fila["receptor_rfc"])),
                # Las contrapartes se guardan aquí, no solo se consultan: sin ellas el
                # expediente sabe CUÁNTO se movió pero no ENTRE QUIÉNES, y no se puede
                # dibujar el rastro del dinero a partir de la evidencia guardada.
                "emisor_rfc": fila["emisor_rfc"] if fila else None,
                "receptor_rfc": fila["receptor_rfc"] if fila else None,
                "cfdi_uuid": None,
            })
        elif tipo == "transferencia":
            fila = _get_bank_tx(str(item.get("tx_id", "")))
            resueltos.append({
                "tipo": "transferencia",
                "tx_id": item.get("tx_id"),
                "monto": float(fila["monto"]) if fila else None,
                "existe": fila is not None,
                "relevante": bool(
                    fila and rfc in (fila["cuenta_origen_rfc"], fila["cuenta_destino_rfc"])
                ),
                "origen_rfc": fila["cuenta_origen_rfc"] if fila else None,
                "destino_rfc": fila["cuenta_destino_rfc"] if fila else None,
                "cfdi_uuid": fila["cfdi_uuid"] if fila else None,
            })
        else:
            resueltos.append({
                "tipo": tipo,
                "uuid": item.get("uuid"),
                "tx_id": item.get("tx_id"),
                "monto": None,
                "existe": False,
                "relevante": False,
                "cfdi_uuid": None,
            })

    # Una factura y la transferencia que la paga son el MISMO dinero: sumar ambas
    # inflaría el monto defraudado. Se cuenta la factura y se marca el pago como
    # evidencia de respaldo, no como monto adicional.
    uuids_citados = {r["uuid"] for r in resueltos if r["tipo"] == "factura" and r["existe"]}
    total = 0.0
    for resuelto in resueltos:
        es_pago_de_factura_citada = (
            resuelto["tipo"] == "transferencia" and resuelto["cfdi_uuid"] in uuids_citados
        )
        resuelto["contado_en_total"] = bool(
            resuelto["existe"] and resuelto["relevante"] and not es_pago_de_factura_citada
        )
        if resuelto["contado_en_total"]:
            total += resuelto["monto"]

    enriquecido["evidencia"] = resueltos
    enriquecido["monto_total_evidencia"] = round(total, 2)
    return enriquecido


def _referenced_invoices(evidencia: list[dict]) -> list[dict]:
    facturas = []
    for item in evidencia:
        if item.get("tipo") == "factura" and item.get("existe"):
            fila = _get_invoice(str(item.get("uuid")))
            if fila:
                facturas.append(fila)
    return facturas


def _referenced_transfers(evidencia: list[dict]) -> list[dict]:
    transferencias = []
    for item in evidencia:
        if item.get("tipo") == "transferencia" and item.get("existe"):
            fila = _get_bank_tx(str(item.get("tx_id")))
            if fila:
                transferencias.append(fila)
    return transferencias


def _verify_efos_69b(rfc_imputado: str, invoices_referenciadas: list[dict]) -> tuple[bool, str]:
    for factura in invoices_referenciadas:
        contraparte = (
            factura["emisor_rfc"] if factura["receptor_rfc"] == rfc_imputado else factura["receptor_rfc"]
        )
        info = tools.check_sat_blacklist(contraparte)
        if info["en_lista_69b"] and info["situacion"] == "DEFINITIVO":
            return True, ""
    return False, (
        "Ninguna de las facturas citadas tiene una contraparte con situación DEFINITIVO "
        "en la lista 69-B del SAT."
    )


def _verify_kickback_circular(rfc_imputado: str) -> tuple[bool, str]:
    for ciclo in tools.find_money_cycles(min_amount=1.0, max_hops=8):
        if rfc_imputado in ciclo["ciclo_rfcs"]:
            return True, ""
    return False, f"No se encontró ningún ciclo de transferencias bancarias que incluya a {rfc_imputado}."


def _verify_materialidad(rfc_imputado: str, invoices_referenciadas: list[dict]) -> tuple[bool, str]:
    for factura in invoices_referenciadas:
        if factura["receptor_rfc"] != rfc_imputado:
            continue
        resultado = tools.verify_service_materiality(rfc_imputado, factura["uuid"])
        if resultado["match"] is False:
            return True, ""
    return False, f"No se encontró una inconsistencia de materialidad verificable para {rfc_imputado}."


def _verify_ingreso_no_declarado(rfc_imputado: str, transfers_referenciadas: list[dict]) -> tuple[bool, str]:
    # El MISMO umbral que usa el triage para nominar. Si aquí se exigiera otro,
    # el sistema seleccionaría casos que luego descarta él solo.
    umbral = tools.umbral_ingreso_sin_factura()
    for transferencia in transfers_referenciadas:
        if transferencia["cuenta_destino_rfc"] != rfc_imputado:
            continue
        if transferencia["cfdi_uuid"] is not None:
            continue
        if float(transferencia["monto"]) >= umbral:
            return True, ""
    return False, (
        f"No se encontró una transferencia recibida por {rfc_imputado} sin CFDI asociado "
        f"por al menos ${umbral:,.2f} (umbral calibrado a los movimientos de este caso)."
    )


def verify_lead(lead_dict: dict) -> tuple[bool, str]:
    lead = enrich_lead(lead_dict)

    campos_requeridos = ["rfc_imputado", "tipo_esquema", "evidencia"]
    faltantes = [c for c in campos_requeridos if c not in lead]
    if faltantes:
        return False, f"{DESCARTADO}: estructura de lead incompleta, faltan campos {faltantes}."

    rfc_imputado: str = lead["rfc_imputado"]
    tipo_esquema: str = lead["tipo_esquema"]
    evidencia: list[dict] = lead["evidencia"]

    if not _entity_exists(rfc_imputado):
        return False, f"{DESCARTADO}: el RFC imputado '{rfc_imputado}' no existe en fraud.db."

    # Se distingue "el Investigador no concluyó nada" de "el Investigador inventó
    # una tipología". El resultado es el mismo (no hay acusación), pero la razón
    # que queda escrita en el expediente NO es la misma, y alguien la va a leer.
    # Antes ambos casos salían como "tipo_esquema '' no es una tipología válida",
    # que se lee como un error del programa en vez de como un dictamen — justo lo
    # que un juez ve si revisa unos libros donde no hay nada que encontrar.
    if not str(tipo_esquema or "").strip():
        return False, (
            f"{DESCARTADO}: el Investigador revisó las operaciones de {rfc_imputado} "
            "y no tipificó ningún esquema de fraude. No hay acusación que sostener."
        )

    if tipo_esquema not in ESQUEMAS_VALIDOS:
        return False, (
            f"{DESCARTADO}: '{tipo_esquema}' no corresponde a ninguna de las tipologías "
            f"reconocidas ({', '.join(sorted(ESQUEMAS_VALIDOS))})."
        )

    if not evidencia:
        return False, f"{DESCARTADO}: no hay evidencia (facturas/transferencias) referenciada."

    inexistentes = [it for it in evidencia if not it.get("existe")]
    if inexistentes:
        primero = inexistentes[0]
        identificador = primero.get("uuid") or primero.get("tx_id") or primero.get("tipo")
        return False, f"{DESCARTADO}: la evidencia citada '{identificador}' no existe en fraud.db."

    ajenas = [it for it in evidencia if it.get("existe") and not it.get("relevante")]
    if ajenas:
        primera = ajenas[0]
        identificador = primera.get("uuid") or primera.get("tx_id")
        return False, (
            f"{DESCARTADO}: la evidencia '{identificador}' existe pero no involucra a "
            f"{rfc_imputado} (ni como emisor/receptor ni como origen/destino del pago)."
        )

    if lead["monto_total_evidencia"] <= 0:
        return False, f"{DESCARTADO}: la evidencia verificada no suma un monto mayor a cero."

    invoices_ref = _referenced_invoices(evidencia)
    transfers_ref = _referenced_transfers(evidencia)

    if tipo_esquema == "EFOS_69B":
        ok, razon = _verify_efos_69b(rfc_imputado, invoices_ref)
    elif tipo_esquema == "KICKBACK_CIRCULAR":
        ok, razon = _verify_kickback_circular(rfc_imputado)
    elif tipo_esquema == "INGRESO_NO_DECLARADO":
        ok, razon = _verify_ingreso_no_declarado(rfc_imputado, transfers_ref)
    else:
        ok, razon = _verify_materialidad(rfc_imputado, invoices_ref)

    if not ok:
        return False, f"{DESCARTADO}: {razon}"

    return True, CONFIRMADO
