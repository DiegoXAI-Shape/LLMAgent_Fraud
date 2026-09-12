"""Validación determinista de RFCs leídos por OCR/visión.

Por qué existe este módulo
--------------------------
La prueba de visión sobre una nómina real (bitácora 15) dejó un resultado
incómodo: el modelo leyó PERFECTO todos los montos y el folio fiscal de 36
caracteres, pero leyó MAL el RFC del emisor — `ADSR51130N5A` en vez de
`ADR531130N5A` — de forma idéntica las 3 veces, a temperatura 0. No es ruido
aleatorio que se arregle repitiendo la lectura: es un error sistemático.

Eso importa más que cualquier otro campo porque el RFC es justo la llave con la
que `check_sat_blacklist` consulta el listado 69-B. Un RFC mal leído no produce
un error: produce un "no está en la lista" que se ve EXACTAMENTE igual que una
revisión limpia. Es el peor tipo de falla en un sistema forense — un falso
negativo silencioso.

La defensa no puede ser pedirle al modelo que lea con más cuidado. Es el mismo
principio que ya gobierna el resto del proyecto: lo que el modelo produce se
verifica con código determinista. Aquí hay dos capas:

1. `rfc_valido` — el último carácter del RFC es un dígito verificador calculado
   a partir de los otros once. Es aritmética pura, no necesita catálogo ni red.
   Medido contra los 11,631 RFCs reales del SAT: lo pasan 11,606 (99.79%).
   Contra errores de un carácter simulados sobre RFCs válidos: detecta 84.65%.

2. `sugerir_rfc` — si el RFC leído no aparece en ningún universo conocido
   (la empresa auditada o el listado 69-B), busca el más cercano por distancia
   de edición. Convierte "no lo encontré" en "¿quisiste decir ADR531130N5A?".

Sobre el 0.21% que falla
------------------------
25 RFCs publicados por el propio SAT no pasan su propio dígito verificador.
Por eso `rfc_valido` NUNCA debe usarse para rechazar de forma automática: un
RFC que falla el checksum se marca para revisión, no se descarta. Descartarlo
convertiría un error de lectura en la pérdida de una empresa realmente listada.
"""

import sqlite3

from config import DB_PATH

# Tabla oficial del SAT para el cálculo del dígito verificador.
_VALORES: dict[str, int] = {c: i for i, c in enumerate("0123456789ABCDEFGHIJKLMN&OPQRSTUVWXYZ")}
_VALORES[" "] = 37
_VALORES["Ñ"] = 38


def digito_verificador(rfc: str) -> str | None:
    """Calcula el dígito verificador que DEBERÍA tener el RFC.

    Devuelve None si la cadena no tiene forma de RFC (largo o caracteres
    inválidos), para poder distinguir "no evaluable" de "mal calculado".
    """
    limpio = rfc.strip().upper()
    if len(limpio) == 12:
        # Las personas morales tienen 12 caracteres; el algoritmo opera sobre 13,
        # así que se rellenan con un espacio a la izquierda (valor 37).
        limpio = " " + limpio
    if len(limpio) != 13:
        return None

    cuerpo = limpio[:12]
    if any(c not in _VALORES for c in cuerpo):
        return None

    suma = sum(_VALORES[c] * (13 - i) for i, c in enumerate(cuerpo))
    residuo = suma % 11
    if residuo == 0:
        return "0"
    if residuo == 1:
        return "A"
    return str(11 - residuo)


def rfc_valido(rfc: str) -> bool | None:
    """True/False según el dígito verificador; None si no es evaluable."""
    esperado = digito_verificador(rfc)
    if esperado is None:
        return None
    return rfc.strip().upper()[-1] == esperado


def _distancia_edicion(a: str, b: str, tope: int) -> int:
    """Distancia de Levenshtein, abandonando en cuanto supera `tope`.

    El corte temprano importa: esto se corre contra ~11,600 RFCs por cada
    identificador leído, y la enorme mayoría de las comparaciones son entre
    cadenas totalmente distintas que se descartan en las primeras filas.
    """
    if abs(len(a) - len(b)) > tope:
        return tope + 1

    fila_previa = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        fila = [i]
        for j, cb in enumerate(b, start=1):
            fila.append(min(
                fila_previa[j] + 1,          # borrado
                fila[j - 1] + 1,             # inserción
                fila_previa[j - 1] + (ca != cb),  # sustitución
            ))
        if min(fila) > tope:
            return tope + 1
        fila_previa = fila
    return fila_previa[-1]


def _universo_conocido() -> set[str]:
    """Todos los RFCs que el sistema ya conoce: la empresa auditada y el 69-B."""
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    try:
        conocidos = {fila[0] for fila in conn.execute("SELECT rfc FROM entities")}
        conocidos |= {fila[0] for fila in conn.execute("SELECT rfc FROM sat_blacklist_69b")}
    finally:
        conn.close()
    return conocidos


def sugerir_rfc(rfc: str, max_distancia: int = 2) -> list[str]:
    """RFCs conocidos parecidos al leído, del más parecido al menos.

    Solo tiene sentido llamarla cuando el RFC leído NO está en el universo
    conocido. Si devuelve algo, lo más probable es que el OCR haya tropezado.
    """
    objetivo = rfc.strip().upper()
    candidatos: list[tuple[int, str]] = []
    for conocido in _universo_conocido():
        distancia = _distancia_edicion(objetivo, conocido, max_distancia)
        if distancia <= max_distancia:
            candidatos.append((distancia, conocido))
    candidatos.sort()
    return [conocido for _, conocido in candidatos]


def revisar_rfc_leido(rfc: str) -> dict:
    """Dictamen completo sobre un RFC que salió de una imagen o un PDF.

    `confiable` es la señal que debe consumir quien ingiere el archivo: si es
    False, el RFC requiere confirmación humana antes de usarse como llave de
    búsqueda. Nunca se descarta solo — ver la nota sobre el 0.21% arriba.
    """
    leido = rfc.strip().upper()
    checksum = rfc_valido(leido)
    conocido = leido in _universo_conocido()

    if conocido:
        return {
            "rfc": leido, "confiable": True, "checksum_ok": checksum,
            "conocido": True, "sugerencias": [],
            "motivo": "El RFC existe en la base (empresa auditada o listado 69-B).",
        }

    sugerencias = sugerir_rfc(leido)

    if checksum is False:
        motivo = (
            f"El dígito verificador no cuadra: el RFC termina en '{leido[-1]}' pero "
            f"debería terminar en '{digito_verificador(leido)}'. Es casi seguro un "
            "error de lectura."
        )
    elif checksum is None:
        motivo = "La cadena no tiene forma de RFC (largo o caracteres inválidos)."
    else:
        motivo = (
            "El dígito verificador cuadra, pero el RFC no aparece en la base. "
            "Puede ser una empresa legítima no registrada, o un error de lectura "
            "que casualmente pasó el checksum."
        )

    if sugerencias:
        motivo += f" RFCs conocidos parecidos: {', '.join(sugerencias[:3])}."

    return {
        "rfc": leido,
        "confiable": checksum is True and not sugerencias,
        "checksum_ok": checksum,
        "conocido": False,
        "sugerencias": sugerencias,
        "motivo": motivo,
    }
