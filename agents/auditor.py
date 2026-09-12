"""Fase 3 — Auditor. Cliente Gemini (temperature=0) que redacta el expediente
pericial final en Markdown a partir de los leads YA verificados por verifier.py.
El Auditor nunca decide culpabilidad ni inventa hechos: solo redacta lo que el
filtro determinista ya confirmó o descartó."""

import os
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

from agents.verifier import ESQUEMAS_VALIDOS
from config import CASE_FILE_PATH, GEMINI_MODEL
from core.db import get_connection

AUDITOR_SYSTEM_INSTRUCTION = """Eres un fiscal/auditor forense fiscal en México. Vas a
redactar un expediente pericial de fraude fiscal a partir de datos YA verificados por
un proceso determinista externo — tú no decides si algo se confirma o se descarta,
eso ya fue decidido. Tu único trabajo es redactar con precisión legal, sin inventar
montos, RFCs, UUIDs ni hechos que no estén en los datos que se te entregan.

Para cada caso CONFIRMADO, escribe una sección con:
- ID de caso, RFC y razón social del sujeto
- Monto total defraudado (usa el monto EXACTO que se te da, sin redondear)
- Tipología del esquema
- Tabla de cadena de evidencia (UUIDs de factura / tx_id bancarios, con sus montos)
- Regla fiscal violada, citada de forma explícita

Para cada lead DESCARTADO, escribe una entrada breve en la sección "Leads Descartados"
con el RFC y la razón exacta de exoneración que se te dio — no la suavices ni la omitas.

Responde ÚNICAMENTE con el Markdown del expediente, sin comentarios adicionales."""


def _client() -> genai.Client:
    return genai.Client(api_key=os.environ["GEMINI_API_KEY"])


def _format_confirmados(confirmados: list[dict[str, Any]]) -> str:
    """Arma el texto que se le entrega al Auditor.

    Los montos se formatean AQUÍ, ya como moneda. Antes se interpolaba el float
    de Python tal cual, así que al prompt llegaba `2245374.0` y el expediente
    salía con `$2245374.0` — sin separadores y con un solo decimal. El número
    era correcto (el Auditor lo copió fiel), pero la fuente del formato feo era
    nuestra, no suya. Se le entrega ya formateado para que no tenga que
    reescribir ninguna cifra: copiar es más seguro que reformatear.
    """
    bloques = []
    for lead in confirmados:
        monto = lead.get("monto_total_evidencia") or 0.0
        lineas_evidencia = []
        for item in lead.get("evidencia") or []:
            identificador = item.get("uuid") or item.get("tx_id") or "—"
            monto_item = item.get("monto")
            monto_item_txt = f"${monto_item:,.2f}" if isinstance(monto_item, (int, float)) else "—"
            suma = "suma al total" if item.get("contado_en_total") else "respaldo, no suma"
            lineas_evidencia.append(
                f"    - {item.get('tipo', '—')} {identificador} por {monto_item_txt} ({suma})"
            )
        bloques.append(
            f"- rfc_imputado: {lead.get('rfc_imputado')}\n"
            f"  razon_social: {lead.get('razon_social')}\n"
            f"  tipo_esquema: {lead.get('tipo_esquema')}\n"
            f"  monto_total_evidencia: ${monto:,.2f} MXN\n"
            f"  regla_fiscal: {lead.get('regla_fiscal')}\n"
            f"  narrativa: {lead.get('narrativa')}\n"
            f"  evidencia:\n" + ("\n".join(lineas_evidencia) or "    (ninguna)") + "\n"
        )
    return "\n".join(bloques) if bloques else "(ninguno)"


def _format_descartados(descartados: list[tuple[dict[str, Any], str]]) -> str:
    bloques = []
    for lead, razon in descartados:
        bloques.append(
            f"- rfc_imputado: {lead.get('rfc_imputado')}\n"
            f"  tipo_esquema_tentativo: {lead.get('tipo_esquema')}\n"
            f"  razon_de_descarte: {razon}\n"
        )
    return "\n".join(bloques) if bloques else "(ninguno)"


def build_prompt(confirmados: list[dict[str, Any]], descartados: list[tuple[dict[str, Any], str]]) -> str:
    return (
        "CASOS CONFIRMADOS CON PRUEBA (ya verificados contra la base de datos):\n"
        f"{_format_confirmados(confirmados)}\n\n"
        "LEADS DESCARTADOS POR FALTA DE EVIDENCIA (ya verificados y rechazados):\n"
        f"{_format_descartados(descartados)}\n\n"
        "Redacta el expediente pericial completo en Markdown siguiendo el formato indicado."
    )


MAX_REINTENTOS_GEMINI = 3
ESPERA_BASE_SEGUNDOS = 2.0


def _tabla_evidencia(evidencia: list[dict[str, Any]] | None) -> list[str]:
    if not evidencia:
        return ["_(sin evidencia listada)_", ""]
    lineas = [
        "| Tipo | Identificador | Monto | Suma al total |",
        "|---|---|---|---|",
    ]
    for item in evidencia:
        identificador = item.get("uuid") or item.get("tx_id") or "—"
        monto = item.get("monto")
        monto_txt = f"${monto:,.2f}" if isinstance(monto, (int, float)) else "—"
        suma = "sí" if item.get("contado_en_total") else "no (respaldo)"
        lineas.append(f"| {item.get('tipo', '—')} | `{identificador}` | {monto_txt} | {suma} |")
    lineas.append("")
    return lineas


def redactar_sin_modelo(confirmados: list[dict[str, Any]],
                        descartados: list[tuple[dict[str, Any], str]]) -> str:
    """Arma el expediente con plantilla, sin ningún LLM.

    El Auditor solo REDACTA: los montos, RFC, UUID y tipologías ya vienen
    verificados contra la base por `verifier.py`. Por eso un expediente sin
    Gemini no es un expediente degradado en su contenido probatorio — es el
    mismo hecho, con peor prosa. Que una caída de un servicio externo tire toda
    la investigación sería el peor modo de falla posible en una demostración
    en vivo, y el más fácil de evitar.
    """
    fecha = datetime.now().strftime("%Y-%m-%d %H:%M")
    partes = [
        "# Expediente Forense de Fraude Fiscal",
        "",
        f"_Generado el {fecha}._",
        "",
        "> **Nota:** este expediente se redactó con plantilla determinista porque el "
        "servicio del Auditor (Gemini) no estuvo disponible. Los hechos, montos e "
        "identificadores son idénticos: provienen de la verificación contra la base "
        "de datos, no de la redacción.",
        "",
        f"**Casos confirmados con prueba:** {len(confirmados)}  ",
        f"**Leads descartados por falta de evidencia:** {len(descartados)}",
        "",
        "---",
        "",
        "## Casos confirmados",
        "",
    ]

    if not confirmados:
        partes += ["_Ninguno._", ""]
    for numero, lead in enumerate(confirmados, start=1):
        monto = lead.get("monto_total_evidencia") or 0.0
        partes += [
            f"### {numero}. {lead.get('rfc_imputado')} — {lead.get('razon_social') or 's/n'}",
            "",
            f"- **Tipología:** {lead.get('tipo_esquema')}",
            f"- **Monto total acreditado:** ${monto:,.2f}",
            f"- **Regla fiscal violada:** {lead.get('regla_fiscal') or 'N/A'}",
            "",
            f"{lead.get('narrativa') or ''}",
            "",
            "**Cadena de evidencia**",
            "",
        ]
        partes += _tabla_evidencia(lead.get("evidencia"))

    partes += ["---", "", "## Leads descartados", ""]
    if not descartados:
        partes += ["_Ninguno._", ""]
    for lead, razon in descartados:
        partes += [
            f"- **{lead.get('rfc_imputado')}** "
            f"(tentativa: {lead.get('tipo_esquema') or 'sin tipificar'}) — {razon}",
        ]
    partes.append("")
    return "\n".join(partes)


def generate_case_file(confirmados: list[dict[str, Any]], descartados: list[tuple[dict[str, Any], str]]) -> str:
    if not confirmados and not descartados:
        return (
            "# Expediente Forense de Fraude Fiscal\n\n"
            "No se investigó ningún RFC en esta corrida (no hay empresas marcadas "
            "como `es_empresa_auditada` en fraud.db).\n"
        )

    prompt = build_prompt(confirmados, descartados)

    # El cliente se crea UNA vez y se guarda en una variable. Escribirlo como
    # `_client().models.generate_content(...)` deja al Client como temporal sin
    # ninguna referencia viva: Python lo recolecta y cierra su sesión HTTP a
    # media llamada, y los tres intentos fallan con "the client has been closed"
    # aunque la API esté perfectamente disponible.
    client = _client()

    # Un 503/429 de Gemini es transitorio por definición ("high demand"), así que
    # se reintenta con espera creciente antes de rendirse.
    for intento in range(1, MAX_REINTENTOS_GEMINI + 1):
        try:
            response = client.models.generate_content(
                model=GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    temperature=0,
                    system_instruction=AUDITOR_SYSTEM_INSTRUCTION,
                ),
            )
            if response.text:
                return response.text
            print(f"Aviso: Gemini respondió vacío (intento {intento}).")
        except Exception as exc:
            print(f"Aviso: falló la llamada a Gemini (intento {intento}/{MAX_REINTENTOS_GEMINI}): {exc}")
        if intento < MAX_REINTENTOS_GEMINI:
            time.sleep(ESPERA_BASE_SEGUNDOS * (2 ** (intento - 1)))

    print("Aviso: el Auditor (Gemini) no está disponible; se redacta el expediente "
          "con la plantilla determinista. Los hechos verificados son los mismos.")
    return redactar_sin_modelo(confirmados, descartados)


def save_case_file(markdown_text: str, path: Path = CASE_FILE_PATH) -> Path:
    path.write_text(markdown_text, encoding="utf-8")
    return path


def persist_cases(confirmados: list[dict[str, Any]], descartados: list[tuple[dict[str, Any], str]]) -> None:
    conn = get_connection()
    try:
        for lead in confirmados:
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO investigation_cases "
                    "(case_id, rfc_imputado, tipo_esquema, monto_total_evidencia, estatus_dictamen, "
                    "justificacion_legal, regla_violada, fecha_dictamen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        lead.get("rfc_imputado"),
                        lead.get("tipo_esquema"),
                        float(lead.get("monto_total_evidencia", 0.0)),
                        "CONFIRMADO_CON_PRUEBA",
                        lead.get("narrativa", ""),
                        lead.get("regla_fiscal", "N/A"),
                        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                print(f"Aviso: no se pudo persistir el caso confirmado de {lead.get('rfc_imputado')}: {exc}")
        for lead, razon in descartados:
            # La lista de tipologías se importa, no se repite: escrita a mano
            # aquí se quedó sin 'INGRESO_NO_DECLARADO' cuando se agregó ese 5º
            # esquema, y un lead descartado de ese tipo perdía su tipología al
            # guardarse. Mismo problema que resolvió config.py con los umbrales.
            tipo_esquema = lead.get("tipo_esquema")
            if tipo_esquema not in ESQUEMAS_VALIDOS:
                tipo_esquema = None
            try:
                conn.execute(
                    "INSERT OR REPLACE INTO investigation_cases "
                    "(case_id, rfc_imputado, tipo_esquema, monto_total_evidencia, estatus_dictamen, "
                    "justificacion_legal, regla_violada, fecha_dictamen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        lead.get("rfc_imputado"),
                        tipo_esquema,
                        float(lead.get("monto_total_evidencia", 0.0) or 0.0),
                        "DESCARTADO_FALTA_EVIDENCIA",
                        razon,
                        lead.get("regla_fiscal") or "N/A",
                        datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                print(f"Aviso: no se pudo persistir el lead descartado de {lead.get('rfc_imputado')}: {exc}")
        conn.commit()
    finally:
        conn.close()
