"""El Defensor: responde preguntas de un juez sobre el expediente ya dictaminado.

El brief pide esto dos veces. En qué construir: *"responde una pregunta sorpresa
sobre su razonamiento"*. Y en el criterio de Judgment: *"¿se niega a acusar a
proveedores que no puede respaldar, y puede defender un hallazgo cuando un juez
pregunta?"*. Es el único momento en que el jurado interactúa directamente con el
agente.

DIFERENCIA CON EL INVESTIGADOR: aquí el modelo no decide nada. El dictamen ya
está cerrado por `verifier.py`. El Defensor solo explica decisiones que ya se
tomaron, y para hacerlo tiene las mismas cuatro herramientas de solo lectura
sobre la base — puede ir a buscar la fila exacta que respalda su respuesta.

LA GUARDA QUE IMPORTA: se recolectan todos los identificadores (UUID y RFC) que
el modelo REALMENTE vio — los del expediente y los que devolvieron las
herramientas — y se revisa la respuesta contra esa lista. Si cita un
identificador que nunca vio, se le corrige y se le pide responder de nuevo. Es
el mismo mecanismo que evitó que el Investigador inventara evidencia (bitácora
9): un agente que inventa un folio para sonar convincente frente a un juez es
peor que uno que admite no saber.
"""

import json
import re
from typing import Any

import ollama

from config import CONTEXT_WINDOW, MAX_OUTPUT_TOKENS, OLLAMA_HOST, OLLAMA_MODEL
from core import tools

MAX_ITERACIONES_DEFENSA = 8
MAX_CORRECCIONES = 2

_UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)
_RFC_PATTERN = re.compile(r"\b[A-ZÑ&]{3,4}\d{6}[A-Z0-9]{3}\b")

SYSTEM_PROMPT = """Eres el agente forense que realizó esta investigación fiscal y ahora
la defiendes ante un auditor que te interroga. El dictamen YA ESTÁ CERRADO por un proceso
de verificación determinista: tú no decides culpables ni cambias montos, solo EXPLICAS
decisiones que ya se tomaron y las respaldas con los registros.

Tienes herramientas de solo lectura sobre la misma base de datos. Úsalas cuando necesites
un dato que no esté en el expediente que se te entregó — es mejor ir a buscar la fila
exacta que responder de memoria.

REGLAS INQUEBRANTABLES:

0. EL DICTAMEN NO SE RELITIGA. Lo que está en CASOS CONFIRMADOS fue imputado; lo que está
   en LEADS DESCARTADOS no lo fue. Jamás digas que NO acusaste a una empresa que aparece
   en confirmados, ni que SÍ acusaste a una que aparece en descartados. Te preguntan por
   qué se decidió lo que se decidió, no si tú lo decidirías otra vez.
   Si al consultar la base encuentras algo que parece contradecir el expediente, dilo
   explícitamente ("el expediente confirma X, pero el registro Y muestra Z") en vez de
   cambiar la conclusión por tu cuenta. Señalar una inconsistencia es útil; desdecirte en
   silencio destruye la credibilidad del expediente completo.
1. NUNCA inventes un UUID, un RFC o una cifra. Usa únicamente los que aparecen en el
   expediente o los que te devolvió una herramienta en esta conversación.
2. Si no puedes respaldar algo con un registro concreto, DILO: "no puedo respaldar eso
   con los registros disponibles". Admitir un límite vale más que una respuesta que suene
   bien y no se sostenga.
3. Si te preguntan por qué NO acusaste a alguien, CITA LA RAZÓN REGISTRADA en el
   expediente, con sus palabras. No inventes un motivo distinto ni más elaborado del que
   quedó asentado. No lo suavices ni te disculpes: descartar por falta de prueba es una
   decisión correcta, no una falla.
4. Responde en español, directo y breve: de 2 a 5 oraciones. Quien te pregunta tiene
   segundos, no minutos. Cita el identificador o el monto exacto que respalda lo que dices.
5. No uses viñetas ni encabezados. Es una respuesta hablada ante un auditor.

El estándar de prueba que aplicó el verificador, por si te preguntan:
- EFOS_69B: alguna factura citada tiene una contraparte con situación DEFINITIVO en el
  listado 69-B del SAT. Un "presunto" NO basta: todavía puede desvirtuarse.
- KICKBACK_CIRCULAR: existe un ciclo cerrado de transferencias que incluye al imputado,
  con montos similares entre tramos.
- INGRESO_NO_DECLARADO: una transferencia recibida sin CFDI que la ampare.
- SIN_MATERIALIDAD: el concepto facturado es genérico y ajeno al giro registrado.
Además, toda la evidencia citada debe existir en la base y debe involucrar al imputado."""


def _formatear_caso(confirmados: list[dict], descartados: list[dict]) -> str:
    """El expediente completo, compacto, como contexto del interrogatorio."""
    partes: list[str] = ["=== CASOS CONFIRMADOS ==="]
    if not confirmados:
        partes.append("(ninguno)")
    for lead in confirmados:
        monto = lead.get("monto_total_evidencia") or 0.0
        partes.append(
            f"\nRFC {lead.get('rfc_imputado')} — {lead.get('razon_social') or 's/n'}\n"
            f"  esquema: {lead.get('tipo_esquema')}\n"
            f"  monto acreditado: ${monto:,.2f} MXN\n"
            f"  regla violada: {lead.get('regla_fiscal') or 'no especificada'}\n"
            f"  hechos: {lead.get('narrativa') or 'sin narrativa'}"
        )
        for item in lead.get("evidencia") or []:
            identificador = item.get("uuid") or item.get("tx_id") or "?"
            monto_item = item.get("monto")
            monto_txt = f"${monto_item:,.2f}" if isinstance(monto_item, (int, float)) else "?"
            computo = "suma al total" if item.get("contado_en_total") else "respaldo, no suma"
            contrapartes = ""
            if item.get("emisor_rfc") or item.get("origen_rfc"):
                origen = item.get("emisor_rfc") or item.get("origen_rfc")
                destino = item.get("receptor_rfc") or item.get("destino_rfc")
                contrapartes = f", de {origen} a {destino}"
            partes.append(
                f"    - {item.get('tipo')} {identificador} por {monto_txt}{contrapartes} ({computo})"
            )

    partes.append("\n=== LEADS DESCARTADOS (revisados y NO imputados) ===")
    if not descartados:
        partes.append("(ninguno)")
    for item in descartados:
        lead = item.get("lead") or {}
        partes.append(
            f"  - RFC {lead.get('rfc_imputado')} "
            f"(tentativa: {lead.get('tipo_esquema') or 'sin tipificar'})\n"
            f"    razón del descarte: {item.get('razon')}"
        )
    return "\n".join(partes)


def _identificadores_del_caso(texto_caso: str) -> set[str]:
    return set(_UUID_PATTERN.findall(texto_caso)) | set(_RFC_PATTERN.findall(texto_caso))


# Frases con las que el modelo niega una imputación. Se buscan en la ventana de
# texto ANTERIOR a la mención del RFC, que es donde cae la negación en español
# ("no acusé a X", "se descartó a X", "no se imputó a X").
_NEGACIONES = (
    "no acus", "no se acus", "no imput", "no se imput", "descart", "se descart",
    "no fue acus", "no fue imput", "exoner", "no hay evidencia contra",
)
_VENTANA_NEGACION = 140


def _contradice_expediente(respuesta: str, confirmados: list[dict]) -> list[str]:
    """RFC que el expediente confirmó pero que la respuesta presenta como no imputados.

    Es una heurística de texto, no un juez semántico: busca una negación en los
    ~140 caracteres previos a la mención del RFC. Puede tener falsos positivos
    (por ejemplo "no se descartó a X"), y por eso el resultado se MUESTRA como
    advertencia en vez de bloquear la respuesta.

    Existe porque la guarda de identificadores no bastaba. En la primera prueba
    real el modelo respondió "no acusé a GDH210804LCC" sobre un caso que el
    expediente daba por confirmado: no inventó ningún dato — invirtió la
    conclusión. Un agente que se desdice de su propia acusación frente a un
    auditor hace más daño que uno que admite no saber.
    """
    texto = respuesta.lower()
    contradichos: list[str] = []
    for lead in confirmados:
        rfc = str(lead.get("rfc_imputado") or "").strip()
        if not rfc:
            continue
        for coincidencia in re.finditer(re.escape(rfc.lower()), texto):
            inicio = max(0, coincidencia.start() - _VENTANA_NEGACION)
            previo = texto[inicio:coincidencia.start()]
            if any(negacion in previo for negacion in _NEGACIONES):
                contradichos.append(rfc)
                break
    return contradichos


def responder_pregunta(
    pregunta: str,
    confirmados: list[dict[str, Any]],
    descartados: list[dict[str, Any]],
    model: str = OLLAMA_MODEL,
    client: ollama.Client | None = None,
) -> dict[str, Any]:
    """Responde una pregunta sobre el expediente.

    Devuelve {"respuesta", "herramientas_usadas", "identificadores_inventados"}.
    `identificadores_inventados` no vacío significa que el modelo citó algo que
    nunca vio y no lo corrigió: la interfaz debe advertirlo en vez de ocultarlo.
    """
    if client is None:
        client = ollama.Client(host=OLLAMA_HOST)

    texto_caso = _formatear_caso(confirmados, descartados)
    vistos = _identificadores_del_caso(texto_caso)

    mensajes: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            f"Este es el expediente que entregaste:\n\n{texto_caso}\n\n"
            f"El auditor pregunta:\n\"{pregunta.strip()}\"\n\n"
            "Responde de forma directa y breve, respaldando lo que digas con los registros."
        )},
    ]

    herramientas_usadas: list[str] = []
    correcciones = 0

    for _ in range(MAX_ITERACIONES_DEFENSA):
        respuesta = client.chat(
            model=model,
            messages=mensajes,
            tools=tools.TOOL_SCHEMAS,
            think=False,
            options={
                "temperature": 0.0,
                "num_predict": MAX_OUTPUT_TOKENS,
                "num_ctx": CONTEXT_WINDOW,
            },
        )
        mensaje = respuesta.message

        if mensaje.tool_calls:
            mensajes.append({
                "role": "assistant",
                "content": mensaje.content or "",
                "tool_calls": mensaje.tool_calls,
            })
            for llamada in mensaje.tool_calls:
                argumentos = dict(llamada.function.arguments or {})
                resultado = tools.execute_tool_call(llamada.function.name, argumentos)
                resultado_json = json.dumps(resultado, ensure_ascii=False, default=str)
                herramientas_usadas.append(f"{llamada.function.name}({argumentos})")
                vistos.update(_UUID_PATTERN.findall(resultado_json))
                vistos.update(_RFC_PATTERN.findall(resultado_json))
                mensajes.append({
                    "role": "tool",
                    "tool_name": llamada.function.name,
                    "content": resultado_json,
                })
            continue

        contenido = (mensaje.content or "").strip()
        citados = set(_UUID_PATTERN.findall(contenido)) | set(_RFC_PATTERN.findall(contenido))
        inventados = sorted(citados - vistos)

        if not inventados:
            return {
                "respuesta": contenido,
                "herramientas_usadas": herramientas_usadas,
                "identificadores_inventados": [],
                "contradice_expediente": _contradice_expediente(contenido, confirmados),
            }

        if correcciones >= MAX_CORRECCIONES:
            # No se descarta la respuesta: se entrega marcada. Ocultarla dejaría al
            # usuario sin nada; entregarla sin advertencia sería peor.
            return {
                "respuesta": contenido,
                "herramientas_usadas": herramientas_usadas,
                "identificadores_inventados": inventados,
                "contradice_expediente": _contradice_expediente(contenido, confirmados),
            }

        correcciones += 1
        mensajes.append({"role": "assistant", "content": contenido})
        mensajes.append({"role": "user", "content": (
            f"Los identificadores {inventados} que citaste NO aparecen ni en el expediente "
            "ni en ningún resultado de herramienta de esta conversación. Inventarlos frente "
            "a un auditor invalida tu defensa. Responde de nuevo usando solo identificadores "
            "reales, o consulta una herramienta para encontrar el dato correcto. Si no "
            "existe, dilo abiertamente."
        )})

    return {
        "respuesta": "No pude construir una respuesta respaldada en los registros dentro del "
                     "límite de consultas disponibles.",
        "herramientas_usadas": herramientas_usadas,
        "identificadores_inventados": [],
        "contradice_expediente": [],
    }


PREGUNTAS_SUGERIDAS = [
    "¿Por qué acusaste a esta empresa y no a otra?",
    "¿Cómo sabes que no fue un error administrativo?",
    "¿Por qué descartaste los otros leads?",
    "¿De dónde sale ese monto exacto?",
    "¿Qué pasa si el proveedor se desvirtúa del listado 69-B?",
]
