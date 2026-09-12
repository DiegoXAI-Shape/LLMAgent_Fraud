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
import re
import tempfile
import unicodedata
from pathlib import Path

import pandas as pd

from config import CONTEXT_WINDOW, OLLAMA_HOST, OLLAMA_MODEL
from core.rfc import revisar_rfc_leido

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


# =====================================================================
# Capa de documentos: PDF, PNG, JPG
# =====================================================================
#
# Un Excel trae una TABLA (muchas filas, columnas con nombre). Un CFDI en PDF o
# foto trae UN documento con etiquetas sueltas. Son problemas distintos, pero
# desembocan en las mismas 5 hojas canónicas, así que viven en este mismo
# archivo: una sola puerta de entrada para cualquier formato.
#
# ORDEN DE PREFERENCIA, de más exacto a menos:
#
#   1. PDF con capa de texto  -> regex sobre el texto real. Exacto y gratis.
#   2. PDF escaneado / imagen -> se rasteriza y se pasa por visión.
#
# El orden NO es una preferencia estética, está medido. El mismo CFDI en PDF y
# en PNG dio resultados distintos: del PDF el RFC del emisor salió exacto
# (`ADR531130N5A`), de la imagen salió mal (`ADSR51130N5A`) en 4 de 4 intentos.
# Por eso la visión es el último recurso, no el primero: si hay texto, el
# modelo no toca el documento.

# Debajo de esto se asume que el PDF es una imagen escaneada sin capa de texto.
UMBRAL_TEXTO_PDF = 200

EXTENSIONES_TABULARES = {".xlsx", ".xls", ".csv"}
EXTENSIONES_PDF = {".pdf"}
EXTENSIONES_IMAGEN = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}

_RE_UUID = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
_RE_RFC = re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b")
_RE_RFC_PAC = re.compile(r"R\.?\s*F\.?\s*C\.?\s*Proveedor\s*:?\s*([A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3})")
_RE_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}")

# El bloque de sellos criptográficos repite el UUID y el RFC del PAC dentro de
# una cadena larguísima. Si se escanea, contamina los candidatos con basura.
_RE_CADENA_ORIGINAL = re.compile(r"Cadena Original.*", re.DOTALL)


def _a_float(texto: str | None) -> float | None:
    if texto is None:
        return None
    try:
        return float(str(texto).replace(",", "").strip())
    except ValueError:
        return None


def _buscar(patron: str, texto: str) -> str | None:
    encontrado = re.search(patron, texto)
    return encontrado.group(1).strip() if encontrado else None


def extraer_texto_pdf(ruta: Path) -> str:
    """Devuelve la capa de texto del PDF (cadena vacía si viene escaneado)."""
    import pymupdf

    with pymupdf.open(ruta) as doc:
        return "\n".join(pagina.get_text() for pagina in doc)


def rasterizar_pdf(ruta: Path, dpi: int = 200) -> list[Path]:
    """Convierte cada página a PNG temporal, para PDFs sin capa de texto."""
    import pymupdf

    salidas: list[Path] = []
    carpeta = Path(tempfile.mkdtemp(prefix="cfdi_"))
    with pymupdf.open(ruta) as doc:
        for numero, pagina in enumerate(doc):
            destino = carpeta / f"pagina_{numero + 1}.png"
            pagina.get_pixmap(dpi=dpi).save(destino)
            salidas.append(destino)
    return salidas


def extraer_campos_cfdi(texto: str) -> dict:
    """Saca los campos de un CFDI a partir de su texto. 100% determinista.

    Ni una sola cifra pasa por el modelo: todo sale de una expresión regular
    sobre el texto que el propio PDF trae dentro. Es el mismo principio que
    gobierna el resto del sistema, aplicado a la puerta de entrada.
    """
    # Los sellos digitales repiten identificadores dentro de un bloque de miles
    # de caracteres en base64; se recorta antes de buscar nada.
    cuerpo = _RE_CADENA_ORIGINAL.sub("", texto)

    rfc_pac = _RE_RFC_PAC.search(texto)
    rfc_pac = rfc_pac.group(1) if rfc_pac else None

    # El PAC (quien timbra el CFDI) NO es parte de la operación facturada.
    # Meterlo a `entities` inventaría una relación comercial que no existe.
    rfcs = [r for r in _RE_RFC.findall(cuerpo) if r != rfc_pac]
    vistos: list[str] = []
    for rfc in rfcs:
        if rfc not in vistos:
            vistos.append(rfc)

    emisor = vistos[0] if vistos else None
    receptor = vistos[1] if len(vistos) > 1 else None

    uuids = _RE_UUID.findall(cuerpo)
    fechas = _RE_ISO.findall(cuerpo)

    campos = {
        "uuid": uuids[0] if uuids else None,
        "emisor_rfc": emisor,
        "receptor_rfc": receptor,
        "rfc_pac_ignorado": rfc_pac,
        "fecha_emision": fechas[0] if fechas else None,
        "subtotal": _a_float(_buscar(r"Subtotal\s*:?\s*([\d,]+\.\d{2})", cuerpo)),
        # (?<![A-Za-z]) evita que "Subtotal:" dispare esta regla, y exigir ":"
        # pegado descarta "Total Percepciones:" y "Total a Pagar:".
        "total": _a_float(_buscar(r"(?<![A-Za-z])Total\s*:\s*([\d,]+\.\d{2})", cuerpo)),
        "metodo_pago": _buscar(r"M[ée]todo de pago\s*:?\s*([A-Z]{3})", cuerpo),
        "forma_pago": _buscar(r"Forma de pago\s*:?\s*(\d{2})", cuerpo),
        "descripcion": _buscar(r"Descripci[óo]n\s*:\s*([^\n]+)", cuerpo),
        "clave_prod_serv": _buscar(r"Clave Prod\.?\s*Ser\.?\s*:?\s*(\d{4,8})", cuerpo),
        "cantidad": _a_float(_buscar(r"Cantidad\s*:?\s*([\d.]+)", cuerpo)),
        "valor_unitario": _a_float(_buscar(r"Valor Unitario\s*:?\s*([\d,]+\.\d{2})", cuerpo)),
        "importe": _a_float(_buscar(r"Importe\s*:?\s*([\d,]+\.\d{2})", cuerpo)),
        "cp_emisor": _buscar(r"Lugar de expedici[óo]n\s*:?\s*(\d{5})", cuerpo),
        "cp_receptor": _buscar(r"Domicilio Fiscal\s*:?\s*(\d{5})", cuerpo),
        "razon_social_emisor": None,
        "razon_social_receptor": None,
    }

    # La razón social del emisor es la línea que precede a su RFC.
    if emisor:
        lineas = [l.strip() for l in cuerpo.splitlines()]
        for i, linea in enumerate(lineas):
            if emisor in linea:
                for anterior in reversed(lineas[:i]):
                    if anterior and not anterior.endswith(":") and len(anterior) > 3:
                        campos["razon_social_emisor"] = anterior
                        break
                break

    empleado = _buscar(r"N[úu]m\. Empleado\s*:?\s*\n?\s*([^\n]+)", cuerpo)
    if empleado:
        campos["razon_social_receptor"] = re.sub(r"^[\d.\-\s]+", "", empleado).strip()

    return campos


# La lectura por visión se parte en pasadas chicas, y no por elegancia: está
# medido. Pidiéndole 4 campos, el modelo devolvió los DOS RFC del CFDI. Pidiéndole
# los mismos campos dentro de una lista de 15, devolvió el JSON completo y bien
# formado pero con `receptor_rfc`, `razon_social_receptor` y los dos códigos
# postales VACÍOS — dos de dos intentos, sin truncarse (374 de 1024 tokens).
# No es un problema de presupuesto ni de formato: entre más campos se le piden
# de golpe, más campos abandona. Tres preguntas cortas recuperan lo que una
# pregunta larga pierde.
_PASADAS_VISION: list[tuple[str, str, str]] = [
    (
        "identificadores",
        '{"uuid":"","emisor_rfc":"","receptor_rfc":""}',
        "El comprobante tiene DOS RFC distintos: el de quien EMITE (la empresa, arriba) "
        "y el de quien RECIBE. Devuelve los dos, cada uno en su llave.",
    ),
    (
        "montos",
        '{"subtotal":0.0,"total":0.0,"cantidad":0.0,"valor_unitario":0.0,"importe":0.0}',
        "Copia los números tal cual, sin separadores de miles.",
    ),
    (
        "descriptivos",
        '{"razon_social_emisor":"","razon_social_receptor":"","fecha_emision":"",'
        '"metodo_pago":"","descripcion":"","cp_emisor":"","cp_receptor":""}',
        "El código postal del emisor aparece como 'Lugar de expedición'.",
    ),
]

_CLAVES_NUMERICAS = ("subtotal", "total", "cantidad", "valor_unitario", "importe")


def _preguntar_a_vision(cliente, rutas_imagen: list[Path], esquema: str, pista: str) -> dict:
    prompt = (
        "Lee este comprobante fiscal (CFDI) mexicano.\n"
        f"{pista}\n"
        "Responde ÚNICAMENTE con un JSON compacto en una sola línea, con estas llaves "
        f"y ninguna más:\n{esquema}\n"
        "Copia los valores EXACTAMENTE como aparecen. Si un campo no está en la imagen, "
        "déjalo vacío. No expliques nada."
    )
    respuesta = cliente.chat(
        model=OLLAMA_MODEL,
        messages=[{
            "role": "user",
            "content": prompt,
            "images": [str(r) for r in rutas_imagen],
        }],
        think=False,
        options={"temperature": 0.0, "num_predict": 512, "num_ctx": CONTEXT_WINDOW},
    )
    contenido = (respuesta.message.content or "").strip()
    inicio, fin = contenido.find("{"), contenido.rfind("}")
    if inicio == -1 or fin == -1:
        return {}
    return json.loads(contenido[inicio:fin + 1])


def extraer_campos_con_vision(rutas_imagen: list[Path]) -> dict:
    """Último recurso: le pide al modelo que lea la imagen, en varias pasadas.

    Todo lo que salga de aquí es sospechoso por definición — está medido que el
    modelo lee los montos perfecto pero rompe los RFC. Por eso el resultado pasa
    íntegro por `revisar_rfc_leido` antes de que nada toque la base.
    """
    try:
        import ollama
    except ImportError:
        return {}

    cliente = ollama.Client(host=OLLAMA_HOST)
    campos: dict = {}
    for _nombre, esquema, pista in _PASADAS_VISION:
        try:
            parcial = _preguntar_a_vision(cliente, rutas_imagen, esquema, pista)
        except Exception:
            continue
        # Una pasada que falla no debe borrar lo que otra ya consiguió, y un
        # campo vacío no debe pisar a uno que sí se leyó.
        for clave, valor in parcial.items():
            if valor not in (None, "") and not campos.get(clave):
                campos[clave] = valor

    for clave in _CLAVES_NUMERICAS:
        if clave in campos:
            campos[clave] = _a_float(campos[clave])
    return campos


def _a_hojas_canonicas(campos: dict, procedencia: str) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Convierte los campos de UN CFDI en las hojas que espera ingest.py."""
    reporte = [f"  origen: {procedencia}"]

    faltantes = [c for c in ("uuid", "emisor_rfc", "receptor_rfc", "total") if not campos.get(c)]
    if faltantes:
        reporte.append(f"  NO se pudo armar la factura: faltan {faltantes}")
        return {}, reporte

    if campos.get("rfc_pac_ignorado"):
        reporte.append(
            f"  RFC {campos['rfc_pac_ignorado']} ignorado: es el PAC que timbra, "
            "no una parte de la operación."
        )

    # Cada RFC leído pasa por el dígito verificador y por el catálogo real.
    for papel in ("emisor_rfc", "receptor_rfc"):
        dictamen = revisar_rfc_leido(str(campos[papel]))
        marca = "OK " if dictamen["confiable"] else "REVISAR"
        reporte.append(f"  [{marca}] {papel}={campos[papel]} -> {dictamen['motivo']}")

    entidades = pd.DataFrame([
        {
            "rfc": campos["emisor_rfc"],
            "razon_social": campos.get("razon_social_emisor") or campos["emisor_rfc"],
            "fecha_constitucion": None,   # el CFDI no la trae; no se inventa
            "representante_legal": None,
            "codigo_postal": campos.get("cp_emisor") or "00000",
            # Se marca al emisor para que el pipeline tenga a quién investigar.
            # Con `--rfc` se puede apuntar a otro.
            "es_empresa_auditada": True,
        },
        {
            "rfc": campos["receptor_rfc"],
            "razon_social": campos.get("razon_social_receptor") or campos["receptor_rfc"],
            "fecha_constitucion": None,
            "representante_legal": None,
            "codigo_postal": campos.get("cp_receptor") or "00000",
            "es_empresa_auditada": False,
        },
    ])
    reporte.append(
        f"  se marcó a {campos['emisor_rfc']} como empresa auditada "
        "(usa --rfc para investigar a otra)."
    )

    facturas = pd.DataFrame([{
        "uuid": campos["uuid"],
        "emisor_rfc": campos["emisor_rfc"],
        "receptor_rfc": campos["receptor_rfc"],
        "fecha_emision": campos.get("fecha_emision"),
        "subtotal": campos.get("subtotal") or campos["total"],
        "total": campos["total"],
        "metodo_pago": (campos.get("metodo_pago") or "PUE")[:3],
        # 99 = "Por definir" en el catálogo del SAT. Es un código real, no un
        # relleno inventado: dice "el documento no lo especifica".
        "forma_pago": campos.get("forma_pago") or "99",
        "estado_cfdi": "VIGENTE",
    }])

    partidas = pd.DataFrame([{
        "invoice_uuid": campos["uuid"],
        "clave_prod_serv": campos.get("clave_prod_serv") or "01010101",
        "descripcion": campos.get("descripcion") or "(sin descripción en el documento)",
        "cantidad": campos.get("cantidad") or 1.0,
        "valor_unitario": campos.get("valor_unitario") or campos["total"],
        "importe": campos.get("importe") or campos["total"],
    }])

    return {"Entidades": entidades, "Facturas": facturas, "Partidas": partidas}, reporte


def cargar_documento(ruta: Path, usar_modelo: bool = True) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """Lee un CFDI suelto en PDF o imagen y lo traduce al esquema canónico."""
    ruta = Path(ruta)
    sufijo = ruta.suffix.lower()

    if sufijo in EXTENSIONES_PDF:
        texto = extraer_texto_pdf(ruta)
        if len(texto) >= UMBRAL_TEXTO_PDF:
            campos = extraer_campos_cfdi(texto)
            return _a_hojas_canonicas(campos, f"PDF con capa de texto ({len(texto)} caracteres), sin modelo")

        if not usar_modelo:
            return {}, ["  PDF escaneado y el modelo está desactivado: no hay de dónde leer."]
        imagenes = rasterizar_pdf(ruta)
        campos = extraer_campos_con_vision(imagenes)
        return _a_hojas_canonicas(campos, f"PDF escaneado -> {len(imagenes)} imagen(es) -> visión")

    if sufijo in EXTENSIONES_IMAGEN:
        if not usar_modelo:
            return {}, ["  Es una imagen y el modelo está desactivado: no hay de dónde leer."]
        campos = extraer_campos_con_vision([ruta])
        return _a_hojas_canonicas(campos, "imagen -> visión")

    raise ValueError(f"cargar_documento no acepta {sufijo}")


def cargar(ruta: Path, usar_modelo: bool = True) -> tuple[dict[str, pd.DataFrame], list[str]]:
    """LA puerta de entrada. Recibe cualquier formato y despacha por extensión.

    El resto del pipeline nunca se entera de si el caso llegó en Excel, en PDF
    o en una foto: de aquí sale siempre el mismo esquema canónico.
    """
    ruta = Path(ruta)
    sufijo = ruta.suffix.lower()

    if sufijo in EXTENSIONES_TABULARES:
        return cargar_tabular(ruta, usar_modelo=usar_modelo)
    if sufijo in EXTENSIONES_PDF | EXTENSIONES_IMAGEN:
        return cargar_documento(ruta, usar_modelo=usar_modelo)

    aceptados = sorted(EXTENSIONES_TABULARES | EXTENSIONES_PDF | EXTENSIONES_IMAGEN)
    raise ValueError(f"Formato no soportado: '{sufijo}'. Se aceptan: {', '.join(aceptados)}")
