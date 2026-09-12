"""Fase 3 — Auditor. Cliente Gemini (temperature=0) que redacta el expediente
pericial final en Markdown a partir de los leads YA verificados por verifier.py.
El Auditor nunca decide culpabilidad ni inventa hechos: solo redacta lo que el
filtro determinista ya confirmó o descartó."""

import os
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from google import genai
from google.genai import types

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
    bloques = []
    for lead in confirmados:
        bloques.append(
            f"- rfc_imputado: {lead.get('rfc_imputado')}\n"
            f"  razon_social: {lead.get('razon_social')}\n"
            f"  tipo_esquema: {lead.get('tipo_esquema')}\n"
            f"  monto_total_evidencia: {lead.get('monto_total_evidencia')}\n"
            f"  regla_fiscal: {lead.get('regla_fiscal')}\n"
            f"  narrativa: {lead.get('narrativa')}\n"
            f"  evidencia: {lead.get('evidencia')}\n"
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


def generate_case_file(confirmados: list[dict[str, Any]], descartados: list[tuple[dict[str, Any], str]]) -> str:
    if not confirmados and not descartados:
        return (
            "# Expediente Forense de Fraude Fiscal\n\n"
            "No se investigó ningún RFC en esta corrida (no hay empresas marcadas "
            "como `es_empresa_auditada` en fraud.db).\n"
        )

    client = _client()
    prompt = build_prompt(confirmados, descartados)
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            system_instruction=AUDITOR_SYSTEM_INSTRUCTION,
        ),
    )
    return response.text


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
            tipo_esquema = lead.get("tipo_esquema")
            if tipo_esquema not in {"EFOS_69B", "KICKBACK_CIRCULAR", "EMPRESA_FACHADA", "SIN_MATERIALIDAD"}:
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
