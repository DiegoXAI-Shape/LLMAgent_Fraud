"""Descarga y parsea los listados oficiales del Artículo 69-B del SAT.

Genera los dos CSV que el resto del proyecto consume:

  data/sat/sat_69b_catalogo_completo.csv  — Definitivos + Presuntos, todo el
      universo real (~11,600 RFCs). Se carga entero a `sat_blacklist_69b` para
      que check_sat_blacklist() reconozca cualquier RFC real.
  data/sat/sat_69b_reference.csv          — subconjunto de DEFINITIVOS con
      publicación en el DOF >= 2024, del que se toma el RFC real que se injerta
      en el esquema EFOS de la demo (fechas recientes = narrativa coherente).

Los archivos originales del SAT vienen en Latin-1 y traen 3 renglones de
preámbulo legal antes del encabezado real de la tabla. Además los nombres de
empresa llevan comas dentro de comillas, así que hay que parsearlos con un
lector de CSV de verdad -- un split por comas corrompe filas.
"""

import argparse
import csv
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen

from config import SAT_69B_REFERENCE_PATH, SAT_CATALOGO_COMPLETO_PATH, SAT_DIR

BASE_URL = "http://omawww.sat.gob.mx/cifras_sat/Documents"

# (archivo, valor de "Situación del contribuyente", situación normalizada,
#  índice de la columna con la fecha de publicación en el DOF que aplica)
LISTAS = [
    ("Definitivos.csv", "Definitivo", "DEFINITIVO", 15),
    ("Presuntos.csv", "Presunto", "PRESUNTO", 7),
]

FILAS_DE_PREAMBULO = 3
ANIO_MINIMO_REFERENCIA = 2024


def descargar(nombre_archivo: str, destino: Path) -> Path:
    url = f"{BASE_URL}/{nombre_archivo}"
    peticion = Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urlopen(peticion, timeout=60) as respuesta:
        crudo = respuesta.read()
    destino.write_bytes(crudo)
    return destino


def parsear(ruta_cruda: Path, situacion_esperada: str, situacion_normalizada: str,
            columna_dof: int) -> list[tuple[str, str, str, str]]:
    texto = ruta_cruda.read_bytes().decode("latin-1")
    filas = list(csv.reader(texto.splitlines()))

    registros = []
    for fila in filas[FILAS_DE_PREAMBULO:]:
        if len(fila) <= columna_dof:
            continue
        rfc, nombre, situacion = fila[1].strip(), fila[2].strip(), fila[3].strip()
        fecha_texto = fila[columna_dof].strip()
        if situacion != situacion_esperada or not rfc or not fecha_texto:
            continue
        try:
            fecha = datetime.strptime(fecha_texto, "%d/%m/%Y").date()
        except ValueError:
            continue
        registros.append((rfc, nombre, situacion_normalizada, fecha.isoformat()))
    return registros


def construir_catalogos(conservar_crudos: bool = False) -> tuple[int, int]:
    SAT_DIR.mkdir(parents=True, exist_ok=True)
    crudos_dir = SAT_DIR / "_crudos"
    crudos_dir.mkdir(exist_ok=True)

    combinado: dict[str, tuple[str, str, str, str]] = {}
    for nombre_archivo, situacion_esperada, situacion_normalizada, columna_dof in LISTAS:
        ruta_cruda = descargar(nombre_archivo, crudos_dir / nombre_archivo)
        for registro in parsear(ruta_cruda, situacion_esperada, situacion_normalizada, columna_dof):
            combinado[registro[0]] = registro

    with open(SAT_CATALOGO_COMPLETO_PATH, "w", encoding="utf-8", newline="") as f:
        escritor = csv.writer(f)
        escritor.writerow(["rfc", "razon_social", "situacion", "publicacion_dof"])
        escritor.writerows(sorted(combinado.values(), key=lambda r: r[0]))

    recientes = [
        r for r in combinado.values()
        if r[2] == "DEFINITIVO" and int(r[3][:4]) >= ANIO_MINIMO_REFERENCIA
    ]
    recientes.sort(key=lambda r: r[3])
    with open(SAT_69B_REFERENCE_PATH, "w", encoding="utf-8", newline="") as f:
        escritor = csv.writer(f)
        escritor.writerow(["rfc", "razon_social", "publicacion_dof"])
        escritor.writerows((r[0], r[1], r[3]) for r in recientes)

    if not conservar_crudos:
        for archivo in crudos_dir.iterdir():
            archivo.unlink()
        crudos_dir.rmdir()

    return len(combinado), len(recientes)


def main() -> None:
    parser = argparse.ArgumentParser(description="Descarga los listados 69-B oficiales del SAT")
    parser.add_argument("--conservar-crudos", action="store_true",
                        help="No borrar los CSV originales descargados del SAT")
    args = parser.parse_args()

    total, recientes = construir_catalogos(conservar_crudos=args.conservar_crudos)
    print(f"Catálogo completo -> {SAT_CATALOGO_COMPLETO_PATH} ({total} RFCs)")
    print(f"Referencia reciente -> {SAT_69B_REFERENCE_PATH} ({recientes} RFCs DEFINITIVOS >= {ANIO_MINIMO_REFERENCIA})")


if __name__ == "__main__":
    main()
