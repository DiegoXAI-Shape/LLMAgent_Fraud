"""CLI: consume core.pipeline.ejecutar_pipeline e imprime cada evento en
terminal. Toda la lógica del orquestador vive en core/pipeline.py -- este
archivo solo decide CÓMO mostrar los eventos, para que main.py (terminal) y
app.py (Streamlit) nunca puedan desincronizarse entre sí."""

import argparse
import sys

from dotenv import load_dotenv

load_dotenv()

import config
from core.pipeline import ejecutar_pipeline


def main() -> None:
    parser = argparse.ArgumentParser(description="Agente Forense de Fraude Fiscal (EFOS/SAT)")
    parser.add_argument("--archivo", "--excel", dest="archivo", default=str(config.EXCEL_PATH),
                         help="Archivo de entrada: .xlsx, .csv, .pdf, .png o .jpg")
    parser.add_argument("--model", default=config.OLLAMA_MODEL, help="Modelo local (Ollama) para el Investigador")
    parser.add_argument("--rfc", action="append", default=None,
                         help="RFC específico a investigar (repetible). Si se omite, investiga todos los es_empresa_auditada.")
    args = parser.parse_args()

    for evento in ejecutar_pipeline(args.archivo, rfcs=args.rfc, model=args.model):
        fase, estado = evento["fase"], evento["estado"]

        if fase == "ingesta":
            if estado == "inicio":
                print(f"[1/4] Ingesta: {evento['mensaje']}")
            elif estado == "progreso":
                print(f"    {evento['mensaje']}")
            elif estado == "ok":
                for tabla, n in evento["datos"]["counts"].items():
                    print(f"    {tabla}: {n} filas")
            elif estado == "error":
                print(f"ERROR de ingesta: {evento['mensaje']}", file=sys.stderr)
                sys.exit(1)

        elif fase == "investigador":
            if estado == "inicio":
                d = evento["datos"]
                print(f"[2/4] Investigador ({d['model']}): investigando {len(d['rfcs'])} RFC(s) -> {d['rfcs']}")
            elif estado == "ok":
                print(f"    {evento['datos']['n_borradores']} borradores generados.")

        elif fase == "verificador":
            if estado == "inicio":
                print("[3/4] Verificador determinista: aplicando filtro de evidencia dura")
            elif estado == "confirmado":
                lead = evento["datos"]["lead"]
                print(f"    CONFIRMADO: {lead.get('rfc_imputado')} ({lead.get('tipo_esquema')})")
            elif estado == "descartado":
                lead, razon = evento["datos"]["lead"], evento["datos"]["razon"]
                print(f"    DESCARTADO: {lead.get('rfc_imputado')} -> {razon}")

        elif fase == "auditor":
            if estado == "inicio":
                print(f"[4/4] Auditor (Gemini, temp=0): redactando expediente final")
            elif estado == "ok":
                print(f"\nExpediente generado -> {evento['datos']['output_path']}")

        elif fase == "fin":
            d = evento["datos"]
            print(f"Confirmados: {d['n_confirmados']} | Descartados: {d['n_descartados']} | "
                  f"Tiempo total: {d['elapsed_seconds']}s")


if __name__ == "__main__":
    main()
