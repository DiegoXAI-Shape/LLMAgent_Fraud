"""Configuración central: rutas, umbrales de detección y modelos.

Todo lo que más de un módulo necesita vive aquí. En particular los umbrales:
antes estaban duplicados entre `investigator` y `verifier` con el mismo número
escrito dos veces, lo que permite que se desincronicen en silencio si alguien
cambia solo uno.
"""

import os
from pathlib import Path

ROOT_DIR = Path(__file__).parent

# --- Artefactos generados (no se versionan: se regeneran con el pipeline) ---
DB_PATH = ROOT_DIR / "fraud.db"
EXCEL_PATH = ROOT_DIR / "auditoria_empresa_input.xlsx"
GIRO_CATALOG_PATH = ROOT_DIR / "giro_catalog.json"
CASE_FILE_PATH = ROOT_DIR / "CASE_FILE.md"

# --- Datos reales del SAT (sí se versionan: son la base reproducible) ---
SAT_DIR = ROOT_DIR / "data" / "sat"
SAT_69B_REFERENCE_PATH = SAT_DIR / "sat_69b_reference.csv"
SAT_CATALOGO_COMPLETO_PATH = SAT_DIR / "sat_69b_catalogo_completo.csv"

# --- Umbrales de los detectores deterministas ---
# Monto mínimo del tramo más chico de un ciclo para considerarlo relevante.
MIN_CICLO_MONTO = 50_000.0
# Un ciclo real de round-tripping regresa casi el mismo monto en cada salto;
# si los tramos difieren más que esta proporción, es comercio coincidental.
MAX_RATIO_MONTO_CICLO = 1.3
# Diferencia mínima entre lo facturado y lo pagado para marcar la factura.
MIN_DISCREPANCIA_PAGO = 1.0
# Monto mínimo de una transferencia recibida SIN CFDI para marcarla.
MIN_INGRESO_SIN_FACTURA = 200_000.0

# --- Modelos ---
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:4b")
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-pro")

# --- Parámetros del loop del Investigador ---
MAX_ITERATIONS = 20
MAX_OUTPUT_TOKENS = 2048
# Ollama carga los modelos con num_ctx=4096 por default, sin importar que el
# modelo soporte 262144. Con 4096 el loop se queda sin ventana hacia el turno
# 9-10 y el modelo ya no tiene espacio para generar el JSON final.
CONTEXT_WINDOW = 32768
MAX_CORRECCIONES_EVIDENCIA = 2
