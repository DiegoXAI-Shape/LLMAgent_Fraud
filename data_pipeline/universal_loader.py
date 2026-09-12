"""Puerta de entrada flexible: recibe un archivo con estructura desconocida y
lo traduce a las 5 hojas canónicas que `ingest.py` ya sabe validar.

El problema que resuelve: `ingest.py` exige nombres de hoja y de columna
exactos. Si el dataset que llega usa "RFC del Emisor" en vez de "emisor_rfc",
o una hoja "Ventas" en vez de "Facturas", truena en el paso 0 -- antes de que
cualquier detección sirva de algo.

Estrategia en 3 capas, de menos a más riesgo:

  1. Coincidencia exacta con el nombre canónico (gratis).
  2. Diccionario de sinónimos sobre el nombre normalizado (determinista, sin
     modelo, sin posibilidad de que algo se invente).
  3. Solo para lo que quedó sin identificar: se le pregunta al modelo.

REGLA CENTRAL DEL DISEÑO: el modelo mapea NOMBRES de columna, nunca toca los
valores. Los datos los mueve pandas copiando la columna completa tal cual, así
que aunque el modelo se equivoque no puede corromper un solo monto -- a lo más
manda una columna al campo equivocado, y ahí la validación de tipos de
`ingest.py` truena de inmediato.
"""

import json
import unicodedata
from pathlib import Path

import pandas as pd

from config import OLLAMA_HOST, OLLAMA_MODEL

# Columnas que `ingest.py` espera por hoja. La primera forma de cada lista es
# el nombre canónico; las demás son sinónimos aceptados (ya normalizados).
ESQUEMA_CANONICO: dict[str, dict[str, list[str]]] = {
    "Entidades": {
        "rfc": ["rfc", "rfccontribuyente", "rfcempresa", "registrofederaldecontribuyentes"],
        "razon_social": ["razonsocial", "nombre", "nombrecontribuyente", "empresa", "denominacionsocial"],
        "fecha_constitucion": ["fechaconstitucion", "fechadeconstitucion", "fechadealta", "fechadeconstitucionempresa", "constitucion"],
        "representante_legal": ["representantelegal", "representante", "apoderado"],
        "codigo_postal": ["codigopostal", "cp", "codpostal"],
        "es_empresa_auditada": ["esempresaauditada", "auditada", "enauditoria"],
    },
    "Lista_69B": {
        "rfc": ["rfc", "rfccontribuyente", "rfcefos"],
        "situacion": ["situacion", "situacioncontribuyente", "estatus", "estado"],
        "publicacion_dof": ["publicaciondof", "fechadof", "fechapublicacion", "fechadepublicacion", "publicacion"],
        "monto_presunto_total": ["montopresuntototal", "montopresunto", "monto"],
    },
    "Facturas": {
        "uuid": ["uuid", "folilofiscal", "foliofiscal", "uuidcfdi", "iduuid", "cfdiuuid"],
        "emisor_rfc": ["emisorrfc", "rfcemisor", "rfcdelemisor", "emisor", "rfcproveedor"],
        "receptor_rfc": ["receptorrfc", "rfcreceptor", "rfcdelreceptor", "receptor", "rfccliente"],
        "fecha_emision": ["fechaemision", "fechadeemision", "fecha", "fechafactura", "fechadefactura"],
        "subtotal": ["subtotal", "importeneto", "baseimponible"],
        "total": ["total", "montototal", "importetotal", "granTotal", "importe"],
        "metodo_pago": ["metodopago", "metododepago", "metodo"],
        "forma_pago": ["formapago", "formadepago", "forma"],
        "estado_cfdi": ["estadocfdi", "estatuscfdi", "estadofactura", "vigencia"],
    },
    "Partidas": {
        "invoice_uuid": ["invoiceuuid", "uuidfactura", "uuid", "foliofiscal", "cfdiuuid"],
        "clave_prod_serv": ["claveprodserv", "claveproductoservicio", "clavesat", "clavedeproductooservicio", "clave"],
        "descripcion": ["descripcion", "concepto", "detalle", "descripcionconcepto"],
        "cantidad": ["cantidad", "qty", "unidades"],
        "valor_unitario": ["valorunitario", "preciounitario", "preciodeunidad", "precio"],
        "importe": ["importe", "importepartida", "subtotalpartida", "total"],
    },
    "Transacciones_Bancarias": {
        "tx_id": ["txid", "idtransaccion", "idmovimiento", "foliomovimiento"],
        "cuenta_origen_rfc": ["cuentaorigenrfc", "rfcorigen", "origen", "rfcordenante", "ordenante"],
        "cuenta_destino_rfc": ["cuentadestinorfc", "rfcdestino", "destino", "rfcbeneficiario", "beneficiario"],
        "fecha_hora": ["fechahora", "fecha", "fechamovimiento", "fechaoperacion", "fechadeoperacion", "fechadelmovimiento"],
        "monto": ["monto", "importe", "cantidad", "montotransferido"],
        "referencia_bancaria": ["referenciabancaria", "referencia", "concepto", "descripcion"],
        "cfdi_uuid": ["cfdiuuid", "uuidcfdi", "uuidfactura", "foliofiscal"],
    },
}

# Cómo se puede llamar cada hoja en un archivo ajeno.
SINONIMOS_HOJA: dict[str, list[str]] = {
    "Entidades": ["entidades", "empresas", "contribuyentes", "padron", "catalogoempresas", "clientes", "proveedores"],
    "Lista_69B": ["lista69b", "69b", "listanegra", "efos", "blacklist", "listasat", "articulo69b"],
    "Facturas": ["facturas", "cfdi", "comprobantes", "ventas", "facturacion", "ingresos", "cfdis"],
    "Partidas": ["partidas", "conceptos", "detalle", "items", "lineas", "detallefactura", "conceptosfactura"],
    "Transacciones_Bancarias": ["transaccionesbancarias", "transacciones", "bancos", "movimientos",
                                 "estadodecuenta", "transferencias", "spei", "movimientosbancarios", "pagos"],
}


def normalizar(texto: str) -> str:
    """minúsculas, sin acentos, solo alfanuméricos: 'RFC del Emisor' -> 'rfcdelemisor'."""
    descompuesto = unicodedata.normalize("NFKD", str(texto).lower())
    sin_acentos = "".join(c for c in descompuesto if not unicodedata.combining(c))
    return "".join(c for c in sin_acentos if c.isalnum())


def mapear_hoja(nombre_hoja: str) -> str | None:
    normalizado = normalizar(nombre_hoja)
    for canonica, sinonimos in SINONIMOS_HOJA.items():
        if normalizado == normalizar(canonica) or normalizado in sinonimos:
            return canonica
    # Coincidencia parcial: "detalle de facturas 2024" contiene "facturas"
    for canonica, sinonimos in SINONIMOS_HOJA.items():
        if any(s in normalizado for s in sinonimos):
            return canonica
    return None


def mapear_columnas(columnas: list[str], hoja_canonica: str) -> tuple[dict[str, str], list[str]]:
    """Devuelve (mapeo original->canónico, columnas que no se pudieron identificar)."""
    esquema = ESQUEMA_CANONICO[hoja_canonica]

    # Una columna puede encajar en más de un campo (ej. "Referencia" suena a
    # referencia_bancaria pero también a un folio). Se recolectan TODOS sus
    # candidatos y luego se le asigna el primero que siga libre -- si se tomara
    # solo el primero y ya estuviera ocupado, la columna se perdería en silencio.
    candidatos: dict[str, list[str]] = {}
    for columna in columnas:
        normalizada = normalizar(columna)
        candidatos[columna] = [
            canonica for canonica, sinonimos in esquema.items()
            if normalizada == normalizar(canonica)
            or normalizada in [normalizar(s) for s in sinonimos]
        ]

    mapeo: dict[str, str] = {}
    sin_identificar: list[str] = []
    for columna in columnas:
        destino = next((c for c in candidatos[columna] if c not in mapeo.values()), None)
        if destino:
            mapeo[columna] = destino
        else:
            sin_identificar.append(columna)

    return mapeo, sin_identificar


def mapear_con_modelo(columnas_sin_identificar: list[str], hoja_canonica: str,
                      muestra: pd.DataFrame) -> dict[str, str]:
    """Capa 3: le pregunta al modelo a qué campo corresponde cada columna huérfana.

    El modelo SOLO devuelve nombres de campo. Nunca ve ni escribe los valores
    que terminarán en la base de datos -- se le muestran 2 filas de ejemplo
    únicamente para que entienda de qué tipo de dato se trata.
    """
    if not columnas_sin_identificar:
        return {}

    try:
        import ollama
    except ImportError:
        return {}

    campos_disponibles = list(ESQUEMA_CANONICO[hoja_canonica].keys())
    ejemplos = {
        col: [str(v) for v in muestra[col].head(2).tolist()]
        for col in columnas_sin_identificar if col in muestra.columns
    }

    prompt = (
        f"Estoy cargando una hoja de datos fiscales del tipo '{hoja_canonica}'.\n"
        f"Mis campos válidos son exactamente estos: {campos_disponibles}\n\n"
        f"Estas columnas no las pude identificar, con ejemplos de su contenido:\n"
        f"{json.dumps(ejemplos, ensure_ascii=False, indent=2)}\n\n"
        "Responde ÚNICAMENTE con un JSON compacto en una línea que mapee cada columna "
        "a uno de mis campos válidos, así: {\"nombre de la columna\": \"campo_valido\"}. "
        "Si una columna no corresponde a ningún campo válido, omítela del JSON. "
        "No inventes campos que no estén en mi lista."
    )

    try:
        cliente = ollama.Client(host=OLLAMA_HOST)
        respuesta = cliente.chat(
            model=OLLAMA_MODEL,
            messages=[{"role": "user", "content": prompt}],
            think=False,
            options={"temperature": 0.0, "num_predict": 512},
        )
        contenido = (respuesta.message.content or "").strip()
        inicio, fin = contenido.find("{"), contenido.rfind("}")
        if inicio == -1 or fin == -1:
            return {}
        propuesto = json.loads(contenido[inicio:fin + 1])
    except Exception:
        return {}

    # El modelo propone; nosotros filtramos. Solo pasan campos que de verdad existen.
    return {
        columna: campo for columna, campo in propuesto.items()
        if columna in columnas_sin_identificar and campo in campos_disponibles
    }


def cargar_tabular(ruta: Path, usar_modelo: bool = True) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Lee un .xlsx o .csv de estructura desconocida y lo traduce al esquema canónico.

    Devuelve (hojas canónicas listas para ingest.py, líneas del reporte de mapeo).
    """
    ruta = Path(ruta)
    if ruta.suffix.lower() in {".xlsx", ".xls"}:
        crudas = pd.read_excel(ruta, sheet_name=None)
    elif ruta.suffix.lower() == ".csv":
        crudas = {ruta.stem: pd.read_csv(ruta)}
    else:
        raise ValueError(f"cargar_tabular solo acepta .xlsx/.xls/.csv, no {ruta.suffix}")

    resultado: dict[str, pd.DataFrame] = {}
    reporte: list[str] = []

    for nombre_hoja, df in crudas.items():
        canonica = mapear_hoja(nombre_hoja)
        if canonica is None:
            canonica = inferir_hoja_por_columnas(list(df.columns))
        if canonica is None:
            reporte.append(f"  hoja '{nombre_hoja}': NO IDENTIFICADA, se ignora")
            continue

        mapeo, huerfanas = mapear_columnas(list(df.columns), canonica)
        if huerfanas and usar_modelo:
            extra = mapear_con_modelo(huerfanas, canonica, df)
            for columna, campo in extra.items():
                if campo not in mapeo.values():
                    mapeo[columna] = campo
                    huerfanas.remove(columna)
                    reporte.append(f"      (modelo) '{columna}' -> {campo}")

        etiqueta = f"  hoja '{nombre_hoja}' -> {canonica}"
        reporte.append(etiqueta if nombre_hoja == canonica else etiqueta + "   [renombrada]")
        for original, destino in mapeo.items():
            if original != destino:
                reporte.append(f"      '{original}' -> {destino}")
        for huerfana in huerfanas:
            reporte.append(f"      '{huerfana}' -> (sin identificar, se ignora)")

        resultado[canonica] = df.rename(columns=mapeo)[list(mapeo.values())]

    return resultado, reporte


def inferir_hoja_por_columnas(columnas: list[str]) -> str | None:
    """Si el nombre de la hoja no dice nada (típico en un CSV suelto), se
    identifica por cuántas de sus columnas encajan en cada esquema."""
    mejor, mejor_puntaje = None, 0
    for canonica in ESQUEMA_CANONICO:
        mapeo, _ = mapear_columnas(columnas, canonica)
        if len(mapeo) > mejor_puntaje:
            mejor, mejor_puntaje = canonica, len(mapeo)
    # Al menos 3 columnas reconocidas para no adivinar a lo tonto.
    return mejor if mejor_puntaje >= 3 else None


def escribir_excel_canonico(hojas: dict[str, pd.DataFrame], destino: Path) -> Path:
    """Guarda las hojas ya traducidas en el formato exacto que espera ingest.py."""
    destino = Path(destino)
    with pd.ExcelWriter(destino, engine="openpyxl") as writer:
        for nombre in ESQUEMA_CANONICO:
            df = hojas.get(nombre, pd.DataFrame(columns=list(ESQUEMA_CANONICO[nombre].keys())))
            df.to_excel(writer, sheet_name=nombre, index=False)
    return destino
