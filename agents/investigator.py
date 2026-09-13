"""Fase 1 — Investigador. Loop ReAct con function calling contra un modelo
local servido por Ollama, vía el endpoint NATIVO /api/chat (no el compatible
con OpenAI /v1: ese ignora silenciosamente el parámetro `think` para modelos
de la familia Qwen3/Qwen3.5, lo que dejaba el modo thinking encendido y
truncaba las respuestas por presupuesto de tokens)."""

import json
import re
from typing import Any

import ollama

from config import (
    CONTEXT_WINDOW,
    MAX_CORRECCIONES_EVIDENCIA,
    MAX_ITERATIONS,
    MAX_OUTPUT_TOKENS,
    MIN_DISCREPANCIA_PAGO,
    OLLAMA_HOST,
    OLLAMA_MODEL,
)
from core import tools


SYSTEM_PROMPT = """Eres un investigador forense de fraude fiscal (EFOS/CFDI) en México.
Tu trabajo es formular hipótesis sobre un RFC específico usando EXCLUSIVAMENTE las
herramientas disponibles, y producir un borrador de expediente. No inventes datos:
todo lo que afirmes debe estar respaldado por el resultado literal de una herramienta.

ESQUEMA EXACTO de fraud.db (usa estos nombres de tabla y columna tal cual en
query_database — no existen otras columnas ni alias, no adivines nombres):

entities(rfc, razon_social, fecha_constitucion, representante_legal, codigo_postal, es_empresa_auditada)
sat_blacklist_69b(rfc, situacion, publicacion_dof, monto_presunto_total)
invoices(uuid, emisor_rfc, receptor_rfc, fecha_emision, subtotal, total, metodo_pago, forma_pago, estado_cfdi)
invoice_items(id, invoice_uuid, clave_prod_serv, descripcion, cantidad, valor_unitario, importe)
bank_ledger(tx_id, cuenta_origen_rfc, cuenta_destino_rfc, fecha_hora, monto, referencia_bancaria, cfdi_uuid)
investigation_cases(case_id, rfc_imputado, tipo_esquema, monto_total_evidencia, estatus_dictamen, justificacion_legal, regla_violada, fecha_dictamen)
v_money_network(source, target, total_transferido, numero_operaciones)          -- vista, agrupa bank_ledger por origen/destino
v_facturas_sin_pago_bancario(uuid, emisor_rfc, receptor_rfc, monto_facturado, monto_pagado, discrepancia)  -- vista

No existe ninguna columna llamada "rfc", "emisor" ni "RFC" en invoices ni en
bank_ledger — en esas dos tablas el RFC siempre va como emisor_rfc/receptor_rfc
o cuenta_origen_rfc/cuenta_destino_rfc. Si una consulta te devuelve un error de
"no such column", NO la repitas: corrígela usando exactamente los nombres de
columna listados arriba.

CHECKLIST OBLIGATORIO — antes de responder con el JSON final DEBES haber
ejecutado, en este orden, TODOS los pasos siguientes (no puedes saltarte
ninguno ni concluir a medio camino):
1. check_sat_blacklist(rfc) para ver si el RFC investigado está en la lista 69-B.
2. query_database para traer TODAS las facturas donde el RFC es emisor_rfc
   O receptor_rfc, y los movimientos de bank_ledger asociados a esas facturas.
3. Por CADA emisor_rfc distinto que aparezca en las facturas del paso 2 donde el
   RFC investigado es el RECEPTOR, llama también check_sat_blacklist sobre ESE
   emisor_rfc. Esto es obligatorio: el esquema EFOS_69B se prueba cuando el
   EMISOR está en situación DEFINITIVO, no cuando lo está el RFC investigado —
   el investigado es quien dedujo facturas de una empresa facturera.
4. find_money_cycles con un min_amount realista (usa el monto de las facturas/
   transferencias que ya viste en el paso 2 como referencia, NUNCA min_amount=1)
   y max_hops=6, para ver si el RFC participa en un ciclo de transferencias
   (round-tripping / kickback circular).
5. Para CADA factura relevante encontrada en el paso 2, verify_service_materiality
   pasándole el rfc investigado y el uuid de esa factura. La herramienta lee el
   concepto real de la base de datos ella sola -- tú NO le pasas ningún texto de
   concepto, solo el uuid.
6. query_database para ver si el RFC investigado recibió alguna transferencia en
   bank_ledger (cuenta_destino_rfc) con cfdi_uuid IS NULL — dinero que entró sin
   ninguna factura que lo respalde es posible ingreso no declarado.

Está PROHIBIDO responder con "evidencia": [] sin haber completado los 6 pasos
del checklist. Si terminas el checklist completo y genuinamente no hay señal
de fraude, entonces sí responde con "evidencia": [] y explica en "narrativa"
qué revisaste y por qué no encontraste nada. Detenerte antes de completar el
checklist es un error grave — no te conformes con el primer resultado.

Después de CADA resultado de herramienta, tu siguiente mensaje debe ser SOLO
una llamada a otra herramienta o el JSON final — NUNCA texto explicando,
resumiendo o narrando el resultado que acabas de recibir. No redactes
resúmenes intermedios en prosa bajo ninguna circunstancia.

Puedes pedir VARIAS herramientas en el mismo turno cuando no dependan una de
otra — por ejemplo, verify_service_materiality para las 3 facturas a la vez, o
check_sat_blacklist de varios emisores juntos. Hazlo siempre que puedas: tienes
un número limitado de turnos y desperdiciarlos en llamadas de una en una hace
que te quedes sin completar el checklist.

Cuando termines de investigar, responde ÚNICAMENTE con un objeto JSON COMPACTO
en UNA SOLA LÍNEA (sin indentación, sin saltos de línea, sin ```json, sin texto
adicional) con esta forma exacta:

{"rfc_imputado": "...", "tipo_esquema": "...", "evidencia": [{"tipo": "factura", "uuid": "..."}, {"tipo": "transferencia", "tx_id": "..."}], "regla_fiscal": "...", "narrativa": "..."}

NO incluyas montos, ni totales, ni razón social: el sistema los resuelve solo,
leyéndolos de la base de datos a partir de los identificadores que tú señales.
Tu trabajo es indicar QUÉ filas son la prueba (el uuid de la factura, el tx_id
de la transferencia), NO cuánto suman. Si escribes cifras, se ignoran.

Cada elemento de "evidencia" DEBE incluir su campo "tipo", con una de estas dos
formas exactas: {"tipo": "factura", "uuid": "..."} o {"tipo": "transferencia",
"tx_id": "..."}. Nunca omitas "tipo".

La lista "evidencia" lleva EXACTAMENTE tantos elementos como pruebas reales
tengas — puede ser uno solo. El ejemplo de arriba muestra dos nada más para
enseñarte las dos formas posibles, NO porque tengan que ser dos. Está PROHIBIDO
rellenar la lista con un elemento vacío o con el identificador en null para que
se parezca al ejemplo: {"tipo": "transferencia", "tx_id": null} no es evidencia,
es basura que invalida el expediente. Si solo tienes una factura, manda una.

Los identificadores se COPIAN LITERALES del resultado de la herramienta: son
cadenas largas tipo "62a25d9c-fbb3-4644-a23f-a06abddfef38". Está PROHIBIDO
inventarlos o usar marcadores de posición como "TX_001", "uuid-1", "1", "2".
Un identificador que no exista en la base de datos invalida todo el expediente.
Si no tienes el identificador exacto a la vista en un resultado de herramienta
de este mismo contexto, vuelve a consultarlo con query_database antes de
responder.

Reglas del campo "tipo_esquema": debe ser EXACTAMENTE UNO (nunca una combinación,
nunca varios separados por "|" ni por coma) de estos 5 valores literales, y lo
eliges SEGÚN LO QUE LAS HERRAMIENTAS REALMENTE DEVOLVIERON, no por intuición:

EFOS_69B — ÚNICAMENTE si check_sat_blacklist sobre la CONTRAPARTE (el emisor de
  una factura que el RFC investigado recibió) devolvió situacion="DEFINITIVO".
  Si ninguna contraparte está en la lista 69-B, está PROHIBIDO usar EFOS_69B.
KICKBACK_CIRCULAR — únicamente si find_money_cycles devolvió un ciclo que
  contiene al RFC investigado.
SIN_MATERIALIDAD — únicamente si verify_service_materiality devolvió match=false
  para algún concepto facturado al RFC investigado.
EMPRESA_FACHADA — como SIN_MATERIALIDAD, pero cuando además la empresa no tiene
  operación real verificable (sin facturas propias, o constituida poco antes de
  las operaciones).
INGRESO_NO_DECLARADO — únicamente si el RFC investigado recibió (cuenta_destino_rfc)
  una transferencia en bank_ledger con cfdi_uuid NULL — dinero real que entró sin
  ninguna factura que lo respalde.

Es NORMAL y ESPERADO que una factura cumpla más de una de estas condiciones a
la vez (ej. una EFOS suele facturar con conceptos vagos, así que también
dispara SIN_MATERIALIDAD). Cuando esto pase, usa esta PRIORIDAD estricta —
elige la primera de esta lista que aplique, no la que detectaste primero:

  1º EFOS_69B              (respaldado por una determinación OFICIAL del SAT —
                             es el cargo más fuerte posible, siempre gana)
  2º KICKBACK_CIRCULAR      (patrón objetivo verificado por grafo, no por texto)
  3º INGRESO_NO_DECLARADO   (un hecho bancario objetivo: la transferencia existe
                             y no tiene factura, no es una inferencia de texto)
  4º EMPRESA_FACHADA
  5º SIN_MATERIALIDAD       (el más débil: es una inferencia nuestra sobre texto,
                             no una determinación oficial ni un patrón numérico)

El campo "narrativa" debe ser UNA sola oración corta (máximo 30 palabras) — el
detalle completo ya está en "evidencia", no lo repitas en prosa larga.

Si al terminar de investigar no encontraste evidencia suficiente para sostener
ninguna tipología, responde igualmente con este mismo formato JSON compacto
pero con "evidencia": [] y "narrativa" explicando en una oración corta por qué
se descarta al RFC.
"""




def get_candidate_rfcs() -> list[str]:
    """Triage de entrada: los 3 "detectores simples" del brief deciden a quién
    vale la pena investigar — el agente nunca recibe una lista de sospechosos
    servida de antemano, la construye él mismo escaneando TODA la base.

    1. Proveedor en la lista 69-B (PRESUNTO o DEFINITIVO): el RECEPTOR de esa
       factura es candidato — pudo haber deducido una operación simulada.
    2. Pago bancario que no cuadra con su factura (vista
       v_facturas_sin_pago_bancario): tanto emisor como receptor son candidatos.
    3. RFC que participa en un ciclo de transferencias bancarias
       (round-tripping / kickback circular).
    """
    candidatos: set[str] = set()

    proveedores_listados = tools.query_database(
        "SELECT DISTINCT i.receptor_rfc AS rfc FROM invoices i "
        "JOIN sat_blacklist_69b b ON b.rfc = i.emisor_rfc "
        "WHERE b.situacion IN ('PRESUNTO', 'DEFINITIVO')"
    )
    candidatos.update(row["rfc"] for row in proveedores_listados)

    pagos_no_cuadran = tools.query_database(
        "SELECT emisor_rfc, receptor_rfc FROM v_facturas_sin_pago_bancario "
        f"WHERE ABS(discrepancia) > {MIN_DISCREPANCIA_PAGO}"
    )
    for fila in pagos_no_cuadran:
        candidatos.add(fila["emisor_rfc"])
        candidatos.add(fila["receptor_rfc"])

    for ciclo in tools.find_money_cycles(min_amount=tools.umbral_ciclo_monto(), max_hops=8):
        candidatos.update(ciclo["ciclo_rfcs"])

    # 4to detector, fuera de los 3 que lista el brief: conceptos vagos sin
    # entregable verificable ("consultoría estratégica", "servicios diversos",
    # "sin especificar"...). Los 3 detectores oficiales no cubren el esquema de
    # falta de materialidad — sin este, esas empresas nunca entran a la lista de
    # "vale la pena investigar". Sigue siendo un detector simple (coincidencia
    # de texto), no un juicio del modelo: solo aparta candidatos, no acusa.
    conceptos_vagos = tools.query_database(
        "SELECT DISTINCT i.receptor_rfc AS rfc FROM invoices i "
        "JOIN invoice_items ii ON ii.invoice_uuid = i.uuid "
        "WHERE LOWER(ii.descripcion) LIKE '%consultor%' "
        "   OR LOWER(ii.descripcion) LIKE '%asesor%' "
        "   OR LOWER(ii.descripcion) LIKE '%estrat%' "
        "   OR LOWER(ii.descripcion) LIKE '%profesionales diversos%' "
        "   OR LOWER(ii.descripcion) LIKE '%sin especificar%' "
        "   OR LOWER(ii.descripcion) LIKE '%servicios diversos%'"
    )
    candidatos.update(row["rfc"] for row in conceptos_vagos)

    # 5to detector: dinero que entra por banco SIN ninguna factura asociada
    # (cfdi_uuid NULL) -- el espejo exacto del detector 2 (factura sin pago),
    # pero en la otra dirección. Es el mismo tipo de señal que "payments that
    # do not match invoices": aquí el pago no coincide con NINGUNA factura.
    ingresos_sin_factura = tools.query_database(
        "SELECT DISTINCT cuenta_destino_rfc AS rfc FROM bank_ledger "
        f"WHERE cfdi_uuid IS NULL AND monto >= {tools.umbral_ingreso_sin_factura()}"
    )
    candidatos.update(row["rfc"] for row in ingresos_sin_factura)

    return sorted(candidatos)


def _extract_json(texto: str) -> dict[str, Any]:
    limpio = texto.strip()
    limpio = re.sub(r"^```(?:json)?", "", limpio).strip()
    limpio = re.sub(r"```$", "", limpio).strip()
    try:
        return json.loads(limpio)
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{.*\}", limpio, re.DOTALL)
    if match:
        return json.loads(match.group(0))

    raise ValueError(f"No se pudo extraer JSON de la respuesta del modelo: {texto[:500]}")


def _default_lead(rfc: str, motivo: str) -> dict[str, Any]:
    return {
        "rfc_imputado": rfc,
        "tipo_esquema": "SIN_MATERIALIDAD",
        "evidencia": [],
        "regla_fiscal": "",
        "narrativa": motivo,
    }


# UUID v4: formato exacto de invoices.uuid y bank_ledger.tx_id en nuestro esquema.
_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)


def _identificadores_citados(evidencia: list[dict]) -> list[str]:
    citados = []
    for item in evidencia:
        valor = item.get("uuid") or item.get("tx_id")
        if valor:
            citados.append(str(valor))
    return citados


def run_tool_loop(rfc: str, client: ollama.Client | None = None, model: str = OLLAMA_MODEL) -> dict[str, Any]:
    if client is None:
        client = ollama.Client(host=OLLAMA_HOST)

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"Investiga al RFC {rfc} y determina si hay evidencia de fraude fiscal."},
    ]
    identificadores_vistos: set[str] = set()
    correcciones = 0

    for _ in range(MAX_ITERATIONS):
        response = client.chat(
            model=model,
            messages=messages,
            tools=tools.TOOL_SCHEMAS,
            think=False,
            options={
                "temperature": 0.0,
                "num_predict": MAX_OUTPUT_TOKENS,
                "num_ctx": CONTEXT_WINDOW,
            },
        )
        mensaje = response.message

        if mensaje.tool_calls:
            messages.append({
                "role": "assistant",
                "content": mensaje.content or "",
                "tool_calls": mensaje.tool_calls,
            })
            for tool_call in mensaje.tool_calls:
                args = dict(tool_call.function.arguments or {})
                resultado = tools.execute_tool_call(tool_call.function.name, args)
                resultado_json = json.dumps(resultado, ensure_ascii=False, default=str)
                identificadores_vistos.update(_UUID_PATTERN.findall(resultado_json))
                messages.append({
                    "role": "tool",
                    "tool_name": tool_call.function.name,
                    "content": resultado_json,
                })
            continue

        contenido = mensaje.content or ""
        try:
            lead = _extract_json(contenido)
        except ValueError:
            return _default_lead(rfc, f"El modelo no devolvió un JSON válido: {contenido[:300]}")

        citados = _identificadores_citados(lead.get("evidencia") or [])
        inventados = [c for c in citados if c not in identificadores_vistos]

        if not inventados:
            return lead

        if correcciones >= MAX_CORRECCIONES_EVIDENCIA:
            lead["evidencia"] = [
                item for item in (lead.get("evidencia") or [])
                if (item.get("uuid") or item.get("tx_id")) not in inventados
            ]
            if not lead["evidencia"]:
                return _default_lead(
                    rfc,
                    f"El modelo citó identificadores inventados ({inventados}) y no los corrigió "
                    "tras dársele la lista de identificadores reales.",
                )
            return lead

        correcciones += 1
        messages.append({"role": "assistant", "content": contenido})
        messages.append({
            "role": "user",
            "content": (
                f"Los identificadores {inventados} que citaste en 'evidencia' NO aparecen en "
                "ningún resultado de herramienta de esta conversación — los inventaste, y eso "
                "invalida el expediente. Los ÚNICOS identificadores reales que has visto hasta "
                f"ahora son: {sorted(identificadores_vistos)[:30]}. Responde de nuevo con el JSON "
                "final usando SOLO identificadores de esa lista (o llama a otra herramienta si "
                "necesitas encontrar la evidencia real antes de responder)."
            ),
        })
        continue

    return _default_lead(rfc, "Se alcanzó el número máximo de iteraciones sin una conclusión del modelo.")


def run_investigation(rfcs: list[str] | None = None, model: str = OLLAMA_MODEL) -> list[dict[str, Any]]:
    if rfcs is None:
        rfcs = get_candidate_rfcs()

    client = ollama.Client(host=OLLAMA_HOST)
    borradores = []
    for rfc in rfcs:
        borradores.append(run_tool_loop(rfc, client=client, model=model))
    return borradores


if __name__ == "__main__":
    resultados = run_investigation()
    print(json.dumps(resultados, ensure_ascii=False, indent=2, default=str))
