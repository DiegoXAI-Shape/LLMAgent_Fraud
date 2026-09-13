"""Orquestador del pipeline completo, como GENERADOR de eventos.

Por qué existe este módulo y no vive todo en `main.py`: el CLI (`main.py`) y el
frontend (`app.py`, Streamlit) necesitan mostrar el MISMO progreso — ingesta,
cada RFC que se investiga, cada confirmación/descarte, el expediente final —
pero de formas distintas (texto en la terminal vs. bloques que se van llenando
en una página web). Si cada uno reimplementara su propia copia del orquestador,
tarde o temprano se desincronizarían, igual que pasó con los umbrales
duplicados entre `investigator.py` y `verifier.py` antes de que existiera
`config.py`. Aquí hay UNA sola versión de "qué hace el pipeline y en qué
orden"; lo único que cambia entre CLI y web es cómo se pinta cada evento.

`ejecutar_pipeline` es un generador: en cada paso relevante hace `yield` de un
diccionario `{"fase", "estado", ...}` y sigue corriendo. Quien lo consume
decide qué hacer con cada evento — imprimirlo, actualizar un widget, ambas.
"""

import time
from pathlib import Path
from typing import Any, Iterator

import ollama
import pandas as pd

import config
from agents import auditor, investigator, verifier
from core import historial
from data_pipeline import universal_loader
from data_pipeline.ingest import REQUIRED_SHEETS, IngestValidationError, run_ingest


def ya_es_canonico(ruta: Path) -> bool:
    """True si el .xlsx ya trae las 5 hojas con el nombre exacto que espera ingest."""
    if ruta.suffix.lower() not in {".xlsx", ".xls"}:
        return False
    try:
        hojas = pd.ExcelFile(ruta).sheet_names
    except Exception:
        return False
    return all(h in hojas for h in REQUIRED_SHEETS)


def preparar_entrada(ruta: Path) -> tuple[Path, list[str]]:
    """Traduce cualquier formato al Excel canónico que `ingest.py` sabe leer.

    Devuelve (ruta_final, reporte_de_traducción). Un archivo que YA viene
    canónico se pasa derecho: es la ruta probada, y hacerlo pasar por el
    traductor solo agregaría una oportunidad de romperlo. Todo lo demás (Excel
    ajeno, CSV, PDF, imagen) entra por `universal_loader`, la única puerta.
    """
    if ya_es_canonico(ruta):
        return ruta, ["formato canónico, se ingiere tal cual"]

    hojas, reporte = universal_loader.cargar(ruta)
    if not hojas:
        raise IngestValidationError(f"No se pudo extraer ninguna tabla de {ruta.name}")

    destino = universal_loader.escribir_excel_canonico(hojas, config.ROOT_DIR / "traducido.xlsx")
    reporte = list(reporte) + [f"traducido -> {destino.name}"]
    return destino, reporte


def ejecutar_pipeline(
    archivo: Path,
    rfcs: list[str] | None = None,
    model: str = config.OLLAMA_MODEL,
    guardar_historial: bool = True,
) -> Iterator[dict[str, Any]]:
    """Corre Ingesta -> Investigador -> Verificador -> Auditor, reportando cada paso.

    Cada evento trae al menos {"fase", "estado"}. `estado` es uno de:
    "inicio" (arrancó la fase), "progreso" (un paso intermedio, ej. un RFC más),
    "ok" (la fase terminó bien), "error" (la fase falló y el generador se detiene).
    """
    t0 = time.time()

    # --- Fase 1: Ingesta ---
    yield {"fase": "ingesta", "estado": "inicio", "mensaje": f"Archivo: {archivo}"}
    try:
        ruta_final, reporte_traduccion = preparar_entrada(Path(archivo))
        for linea in reporte_traduccion:
            yield {"fase": "ingesta", "estado": "progreso", "mensaje": linea}
        counts = run_ingest(ruta_final)
    except (IngestValidationError, FileNotFoundError, ValueError) as exc:
        yield {"fase": "ingesta", "estado": "error", "mensaje": str(exc)}
        return
    yield {"fase": "ingesta", "estado": "ok", "datos": {"counts": counts}}

    # --- Fase 2: Investigador ---
    rfcs_a_investigar = rfcs if rfcs else investigator.get_candidate_rfcs()
    yield {
        "fase": "investigador", "estado": "inicio",
        "datos": {"rfcs": rfcs_a_investigar, "model": model},
    }
    cliente_ollama = ollama.Client(host=config.OLLAMA_HOST)
    borradores = []
    for rfc in rfcs_a_investigar:
        yield {"fase": "investigador", "estado": "progreso", "mensaje": f"investigando {rfc}"}
        borrador = investigator.run_tool_loop(rfc, client=cliente_ollama, model=model)
        borradores.append(borrador)
        yield {"fase": "investigador", "estado": "parcial", "datos": {"rfc": rfc, "borrador": borrador}}
    yield {"fase": "investigador", "estado": "ok", "datos": {"n_borradores": len(borradores)}}

    # --- Fase 3: Verificador determinista ---
    yield {"fase": "verificador", "estado": "inicio"}
    confirmados: list[dict[str, Any]] = []
    descartados: list[tuple[dict[str, Any], str]] = []
    for borrador in borradores:
        lead = verifier.enrich_lead(borrador)
        ok, razon = verifier.verify_lead(lead)
        if ok:
            confirmados.append(lead)
            yield {"fase": "verificador", "estado": "confirmado", "datos": {"lead": lead}}
        else:
            descartados.append((lead, razon))
            yield {"fase": "verificador", "estado": "descartado", "datos": {"lead": lead, "razon": razon}}
    yield {
        "fase": "verificador", "estado": "ok",
        "datos": {"n_confirmados": len(confirmados), "n_descartados": len(descartados)},
    }

    # Se persiste ANTES de la llamada de red al Auditor: el dictamen ya está
    # decidido por el verificador determinista, y perderlo porque Gemini se
    # cayó sería tirar todo el trabajo de las fases 1-3 (ver bitácora 19).
    auditor.persist_cases(confirmados, descartados)

    # --- Fase 4: Auditor ---
    yield {"fase": "auditor", "estado": "inicio"}
    markdown, redactado_por = auditor.generate_case_file(confirmados, descartados)
    output_path = auditor.save_case_file(markdown)

    descartados_serializables = [{"lead": lead, "razon": razon} for lead, razon in descartados]

    expediente_id = None
    if guardar_historial:
        # Archivar no debe poder tumbar una investigación que ya salió bien: si
        # el historial falla, se avisa y se sigue. El expediente en disco y los
        # dictámenes en `investigation_cases` ya están guardados a estas alturas.
        try:
            expediente_id = historial.guardar_expediente(
                confirmados=confirmados,
                descartados=descartados_serializables,
                markdown=markdown,
                redactado_por=redactado_por,
                archivo_origen=Path(archivo).name,
            )
        except Exception as exc:
            yield {"fase": "auditor", "estado": "progreso",
                   "mensaje": f"No se pudo archivar el expediente en el historial: {exc}"}

    yield {
        "fase": "auditor", "estado": "ok",
        "datos": {
            "markdown": markdown,
            "output_path": str(output_path),
            "redactado_por": redactado_por,
            "expediente_id": expediente_id,
        },
    }

    yield {
        "fase": "fin", "estado": "ok",
        "datos": {
            "n_confirmados": len(confirmados),
            "n_descartados": len(descartados),
            "elapsed_seconds": round(time.time() - t0, 1),
            "confirmados": confirmados,
            "descartados": descartados_serializables,
            "redactado_por": redactado_por,
            "expediente_id": expediente_id,
            "archivo_origen": Path(archivo).name,
        },
    }
