"""Pipeline orquestador: Ingesta -> Investigador -> Verificador determinista -> Auditor."""

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import pandas as pd

import config
from agents import auditor, investigator, verifier
from data_pipeline import universal_loader
from data_pipeline.ingest import REQUIRED_SHEETS, IngestValidationError, run_ingest


def _ya_es_canonico(ruta: Path) -> bool:
    """True si el .xlsx ya trae las 5 hojas con el nombre exacto que espera ingest."""
    if ruta.suffix.lower() not in {".xlsx", ".xls"}:
        return False
    try:
        hojas = pd.ExcelFile(ruta).sheet_names
    except Exception:
        return False
    return all(h in hojas for h in REQUIRED_SHEETS)


def preparar_entrada(ruta: Path) -> Path:
    """Traduce cualquier formato al Excel canónico que `ingest.py` sabe leer.

    Un archivo que YA viene canónico se pasa derecho: es la ruta probada, y
    hacerlo pasar por el traductor solo agregaría una oportunidad de romperlo.
    Todo lo demás (Excel ajeno, CSV, PDF, imagen) entra por `universal_loader`,
    que es la única puerta del sistema.
    """
    if _ya_es_canonico(ruta):
        print(f"    formato canónico, se ingiere tal cual")
        return ruta

    hojas, reporte = universal_loader.cargar(ruta)
    for linea in reporte:
        print(linea)
    if not hojas:
        raise IngestValidationError(f"No se pudo extraer ninguna tabla de {ruta.name}")

    destino = universal_loader.escribir_excel_canonico(hojas, config.ROOT_DIR / "traducido.xlsx")
    print(f"    traducido -> {destino.name}")
    return destino


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente Forense de Fraude Fiscal (EFOS/SAT)")
    parser.add_argument("--archivo", "--excel", dest="archivo", default=str(config.EXCEL_PATH),
                         help="Archivo de entrada: .xlsx, .csv, .pdf, .png o .jpg")
    parser.add_argument("--model", default=config.OLLAMA_MODEL, help="Modelo local (Ollama) para el Investigador")
    parser.add_argument("--rfc", action="append", default=None,
                         help="RFC específico a investigar (repetible). Si se omite, investiga todos los es_empresa_auditada.")
    args = parser.parse_args()

    t0 = time.time()

    print(f"[1/4] Ingesta: {args.archivo}")
    try:
        counts = run_ingest(preparar_entrada(Path(args.archivo)))
    except (IngestValidationError, FileNotFoundError, ValueError) as exc:
        print(f"ERROR de ingesta: {exc}", file=sys.stderr)
        sys.exit(1)
    for tabla, n in counts.items():
        print(f"    {tabla}: {n} filas")

    rfcs = args.rfc if args.rfc else investigator.get_candidate_rfcs()
    print(f"[2/4] Investigador ({args.model}): investigando {len(rfcs)} RFC(s) -> {rfcs}")
    borradores = investigator.run_investigation(rfcs, model=args.model)
    print(f"    {len(borradores)} borradores generados.")

    print("[3/4] Verificador determinista: aplicando filtro de evidencia dura")
    confirmados = []
    descartados = []
    for borrador in borradores:
        lead = verifier.enrich_lead(borrador)
        ok, razon = verifier.verify_lead(lead)
        if ok:
            confirmados.append(lead)
            print(f"    CONFIRMADO: {lead.get('rfc_imputado')} ({lead.get('tipo_esquema')})")
        else:
            descartados.append((lead, razon))
            print(f"    DESCARTADO: {lead.get('rfc_imputado')} -> {razon}")

    print("[4/4] Auditor (Gemini, temp=0): redactando expediente final")
    markdown = auditor.generate_case_file(confirmados, descartados)
    auditor.persist_cases(confirmados, descartados)
    output_path = auditor.save_case_file(markdown)

    elapsed = time.time() - t0
    print(f"\nExpediente generado -> {output_path}")
    print(f"Confirmados: {len(confirmados)} | Descartados: {len(descartados)} | Tiempo total: {elapsed:.1f}s")


if __name__ == "__main__":
    main()
