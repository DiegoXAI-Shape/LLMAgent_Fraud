"""Pipeline orquestador: Ingesta -> Investigador -> Verificador determinista -> Auditor."""

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

import config
from agents import auditor, investigator, verifier
from data_pipeline.ingest import IngestValidationError, run_ingest


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente Forense de Fraude Fiscal (EFOS/SAT)")
    parser.add_argument("--excel", default=str(config.EXCEL_PATH), help="Ruta al Excel de entrada")
    parser.add_argument("--model", default=config.OLLAMA_MODEL, help="Modelo local (Ollama) para el Investigador")
    parser.add_argument("--rfc", action="append", default=None,
                         help="RFC específico a investigar (repetible). Si se omite, investiga todos los es_empresa_auditada.")
    args = parser.parse_args()

    t0 = time.time()

    print(f"[1/4] Ingesta: {args.excel}")
    try:
        counts = run_ingest(Path(args.excel))
    except (IngestValidationError, FileNotFoundError) as exc:
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
