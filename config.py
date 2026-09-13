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
#
# Los dos umbrales de monto son RELATIVOS al dataset, no cifras en pesos. Antes
# eran absolutos ($50,000 y $200,000), calibrados contra el dataset de
# demostración. Se midió qué pasaba con los mismos fraudes a otra escala:
#
#     escala 1.00  (anillo $805,807, depósito $907,986)  ->  3 de 3 detectados
#     escala 0.20  (anillo $168,702, depósito $215,597)  ->  3 de 3 detectados
#     escala 0.05  (anillo  $34,468, depósito  $50,989)  ->  1 de 3 detectados
#
# A escala chica el triage nominaba un solo RFC: una pantalla casi vacía,
# indistinguible de "aquí no hay fraude". "Monto grande" no significa lo mismo
# para un corporativo que para una PyME, así que ahora se toma un percentil de
# los movimientos reales de la empresa auditada (ver tools.umbral_monto_movimientos).

# Percentil de los movimientos bancarios que debe superar el tramo más chico de
# un ciclo. La mediana basta porque el filtro de ratio de abajo ya descarta los
# ciclos de comercio coincidental.
PERCENTIL_CICLO_MONTO = 0.50
PISO_CICLO_MONTO = 1_000.0

# Un ciclo real de round-tripping regresa casi el mismo monto en cada salto;
# si los tramos difieren más que esta proporción, es comercio coincidental.
# Es una PROPORCIÓN, así que ya era independiente de la escala.
MAX_RATIO_MONTO_CICLO = 1.3

# Diferencia mínima entre lo facturado y lo pagado para marcar la factura. Un
# peso: no es un umbral de escala sino de redondeo, así que se queda absoluto.
MIN_DISCREPANCIA_PAGO = 1.0

# Percentil que debe superar un depósito recibido SIN CFDI. Más alto que el de
# ciclos porque esta señal es más ruidosa: cualquier movimiento sin factura
# asociada la dispara, y hay motivos legítimos para que eso pase.
PERCENTIL_INGRESO_SIN_FACTURA = 0.75
PISO_INGRESO_SIN_FACTURA = 5_000.0

# --- Modelos ---
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen3.5:4b")
# El default importa: quien clone el repo sin un .env propio se queda con este
# valor. Se midió contra la API real -- 'gemini-2.5-pro' devuelve 429
# (cuota agotada) y 'gemini-2.5-flash' devuelve 404 (ya no está disponible),
# así que un default con cualquiera de esos dos dejaba el proyecto roto para
# cualquiera que no tuviera exactamente nuestro .env.
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.8-flash")

# --- Parámetros del loop del Investigador ---
MAX_ITERATIONS = 20
MAX_OUTPUT_TOKENS = 2048
# Ollama carga los modelos con num_ctx=4096 por default, sin importar que el
# modelo soporte 262144. Con 4096 el loop se queda sin ventana hacia el turno
# 9-10 y el modelo ya no tiene espacio para generar el JSON final.
CONTEXT_WINDOW = 32768
MAX_CORRECCIONES_EVIDENCIA = 2
