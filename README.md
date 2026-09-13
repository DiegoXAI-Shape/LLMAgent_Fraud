# Agente Forense de Fraude Fiscal (EFOS/SAT)

Sistema de dos fases que investiga un dataset de facturación + movimientos
bancarios, decide por sí mismo a quién vale la pena investigar, y solo acusa
cuando puede probarlo con una fila exacta de la base de datos.

Reto: *"Given a company's books and only the hint that something is wrong, can
an AI agent find the fraud, follow the money, and prove it, without accusing
anyone it cannot back up?"*

## Arquitectura

```
Excel (auditoria_empresa_input.xlsx)
        │  ingest.py — pandas, sin LLM, valida tipos/nulos
        ▼
   fraud.db (SQLite)
        │
        ├── TRIAGE (investigator.get_candidate_rfcs) ── 4 detectores simples,
        │   sin LLM: proveedor en lista 69-B, pago que no cuadra, dinero en
        │   círculo, concepto vago sin entregable. Deciden a quién mirarle.
        │
        ▼
   INVESTIGADOR (investigator.py) — Qwen3.5:4b local vía Ollama, tool-calling
        │   nativo. Solo puede pedir 4 herramientas (tools.py): query_database,
        │   find_money_cycles, check_sat_blacklist, verify_service_materiality.
        │   Entrega identificadores (uuid/tx_id), nunca montos.
        ▼
   VERIFICADOR (verifier.py) — código puro, sin LLM. Resuelve montos y razón
        │   social por SQL (enrich_lead), y solo confirma si cada fila citada
        │   existe, pertenece al acusado, y tiene el vínculo probatorio de su
        │   tipología (69-B DEFINITIVO / ciclo cerrado / materialidad rota).
        ▼
   AUDITOR (auditor.py) — Gemini, temperature=0. Redacta el expediente en
        Markdown a partir de los casos YA verificados; no decide nada.
```

## Setup

```bash
python -m venv venv
venv\Scripts\activate          # Windows (en bash: source venv/Scripts/activate)
pip install -r requirements.txt
ollama pull qwen3.5:4b
cp .env.example .env                            # llena GEMINI_API_KEY
```

Correr el pipeline completo:

```bash
python -m data_pipeline.generate_mock_data   # genera el Excel de demo
python -m data_pipeline.ingest               # Excel -> fraud.db
python main.py                               # investigación + verificación + expediente
```

Refrescar los listados del SAT desde la fuente oficial (opcional, ya vienen
versionados en `data/sat/`):

```bash
python -m data_pipeline.sat_downloader
```

## Estructura

```
.
├── main.py                  # orquestador del pipeline completo
├── config.py                # rutas, umbrales de detección y modelos (fuente única)
├── core/
│   ├── db.py                # DDL de fraud.db (6 tablas + 2 vistas), conexiones
│   └── tools.py             # las 4 herramientas deterministas + sus JSON schemas
├── agents/
│   ├── investigator.py      # triage de candidatos (5 detectores) + loop ReAct
│   ├── verifier.py          # resuelve montos por SQL + reglas de rechazo duras
│   └── auditor.py           # redacta el expediente final con Gemini
├── data_pipeline/
│   ├── universal_loader.py    # traduce archivos de estructura desconocida al esquema canónico
│   ├── generate_mock_data.py  # Excel sintético con los 5 esquemas sembrados
│   ├── ingest.py              # Excel -> fraud.db, valida tipos/nulos, sin LLM
│   └── sat_downloader.py      # descarga y parsea los listados 69-B oficiales
└── data/sat/
    ├── sat_69b_catalogo_completo.csv  # 11,631 RFCs reales (Definitivos + Presuntos)
    └── sat_69b_reference.csv          # 502 DEFINITIVOS con DOF >= 2024, para el injerto
```

Los artefactos generados (`fraud.db`, `auditoria_empresa_input.xlsx`,
`giro_catalog.json`, `CASE_FILE.md`) no se versionan: se reconstruyen con los
comandos de arriba.

---

## Bitácora de mejoras

Esto documenta cada bug real que encontramos corriendo el sistema contra
datos reales — no es una lista de features, es la secuencia de diagnósticos
con evidencia, en el orden en que pasaron.

### 1. El modo "thinking" se quedaba encendido y truncaba las respuestas

**Síntoma:** el Investigador regresaba `"El modelo no devolvió un JSON
válido"` o agotaba las iteraciones sin concluir, en la mayoría de las
investigaciones.

**Diagnóstico:** el cliente usaba el endpoint compatible con OpenAI
(`/v1/chat/completions`) de Ollama. Confirmamos con una llamada directa que
`extra_body={"think": False}` **se ignora silenciosamente** para modelos
Qwen3/Qwen3.5 en ese endpoint — el campo `reasoning` seguía apareciendo
completo en la respuesta, turno tras turno, gastando tokens en pensar antes
de responder.

**Fix:** cambiar a `ollama.Client().chat()`, el endpoint **nativo**
`/api/chat`, con `think=False` explícito — ahí sí se respeta.

### 2. La causa real del truncamiento: ventana de contexto de 4096, no el modo thinking

**Síntoma:** después del fix anterior, seguían apareciendo respuestas
cortadas a la mitad (`done_reason: "length"`) en investigaciones largas (9-10
turnos).

**Diagnóstico:** `ollama ps` mostró `CONTEXT 4096` mientras el modelo tenía
cargado — a pesar de que `qwen3.5:4b` soporta 262,144 tokens de contexto.
Ollama usa 4096 por default salvo que se pida explícito. Con el historial de
resultados de herramientas acumulado, el prompt llenaba la ventana hacia el
turno 9 y ya no quedaba espacio para generar ni un JSON corto.

**Fix:** `num_ctx=32768` explícito en cada llamada.

### 3. El checklist obligatorio se quedaba sin turnos

**Síntoma:** con la ventana de contexto ya arreglada, varias investigaciones
seguían agotando `MAX_ITERATIONS` sin concluir.

**Diagnóstico:** el checklist de 5 pasos obligatorios (69-B propio, facturas,
69-B de cada contraparte, ciclos, materialidad por cada factura) necesita más
de 10 turnos si el modelo llama una herramienta a la vez.

**Fix:** `MAX_ITERATIONS` de 10 a 20, y se le permitió explícitamente pedir
varias herramientas independientes en un mismo turno.

### 4. El modelo parafraseaba montos, conceptos e identificadores

**Síntoma:** casos con el patrón de fraude correcto en la narrativa, pero
rechazados porque el monto citado no coincidía con el real en la base
(ej. citó `$706,646.51` cuando la factura real era `$819,709.95`).

**Diagnóstico:** un modelo de 4B no reproduce cifras largas de forma
confiable — es una limitación conocida de generación token por token, no un
prompt mal escrito. Pedirle "cópialo exacto" no lo resuelve.

**Fix estructural:** se le quitó la posibilidad de equivocarse en eso. El
Investigador ahora solo entrega **identificadores** (`uuid` / `tx_id`) en
`evidencia` — nunca montos, nunca razón social. `verifier.enrich_lead()`
resuelve ambos con `SELECT` directo a `fraud.db`. Ninguna cifra del
expediente pasa por generación de texto del LLM.

### 5. `verify_service_materiality` daba falsos negativos: todo "coincidía"

**Síntoma:** una factura con concepto *"Consultoría estratégica en fusiones y
adquisiciones corporativas"* (inventado a propósito para no tener nada que
ver con ningún giro) devolvía `match: true`.

**Diagnóstico:** la heurística comparaba palabras de más de 4 letras, y la
palabra **"servicios" aparece en la descripción de los 5 giros** del
catálogo — cualquier concepto que empezara con "Servicios de…" coincidía con
cualquier giro por accidente.

**Fix:** se reescribió el criterio a dos condiciones: (a) el concepto
corresponde al giro registrado, o (b) es un concepto **específico y
verificable** aunque sea de otro giro (comprar publicidad si eres
transportista es comercio normal). Solo se marca falta de materialidad
cuando el concepto es a la vez ajeno al giro **y** genérico sin entregable
("consultoría", "asesoría estratégica", "servicios profesionales diversos").
Verificado con 6 casos de prueba manuales, los 6 correctos.

### 6. El verificador aceptaba evidencia de otra empresa

**Síntoma (el más grave que encontramos):** el Investigador citó una factura
real — pero de **otra empresa** — para inflar el monto defraudado de
`HCZ181008TJY` de $819,709.95 a $1,095,514.52. El verificador la dejó pasar
porque solo comprobaba que el UUID existiera en la base, no que perteneciera
al acusado.

**Fix:** cada fila de evidencia ahora se marca `relevante` solo si el RFC
acusado aparece como `emisor_rfc`/`receptor_rfc` (facturas) o
`cuenta_origen_rfc`/`cuenta_destino_rfc` (transferencias). Evidencia que
existe pero no involucra al acusado descarta el caso completo.

### 7. La selección de a quién investigar dependía de una bandera que nosotros pusimos

**Síntoma:** ningún bug — pero al releer el brief contra el código, la lista
de candidatos venía de `WHERE es_empresa_auditada = 1`, una columna que
**nosotros** marcamos a mano en los datos sintéticos. En un dataset real del
jurado esa columna no significaría nada.

**El brief pide explícitamente:** *"Use simple detectors (blacklisted
suppliers, payments that do not match invoices, money that moves in a
circle) to point the agent at what is worth digging into."*

**Fix:** `get_candidate_rfcs()` ahora corre 4 detectores sobre **toda** la
base y construye la lista él solo:
1. Receptor de una factura de un proveedor en lista 69-B (PRESUNTO/DEFINITIVO).
2. Emisor o receptor de una factura cuyo pago bancario no cuadra
   (`v_facturas_sin_pago_bancario`).
3. Participante en un ciclo de transferencias bancarias.
4. Receptor de una factura con concepto genérico sin entregable — el brief
   no cubre el esquema de falta de materialidad con sus 3 detectores, así
   que se agregó un 4to, con el mismo criterio de "detector simple" (texto,
   no juicio del modelo).

### 8. El detector de ciclos marcaba 38 "fraudes" que eran comercio normal

**Síntoma:** al activar el detector 3 contra las 20 empresas normales (sin
ningún esquema de fraude), `find_money_cycles` regresó **38 ciclos**. El
sistema habría marcado 19 de 26 empresas (73%) como sospechosas.

**Diagnóstico:** con 20 empresas y 50 transacciones al azar, el dinero
tarde o temprano vuelve a dar la vuelta por pura coincidencia estadística —
eso no es lavado, es densidad de grafo. La diferencia real: el round-tripping
genuino regresa **casi el mismo monto en cada salto** (menos una comisión
pequeña); el comercio normal no tiene ninguna razón para que los montos de
una cadena de 4-8 saltos se parezcan entre sí.

**Fix:** `find_money_cycles` ahora exige `max(montos_tramo) / min(montos_tramo)
<= 1.3` para reportar un ciclo. Resultado verificado: de 38 ciclos, quedó
exactamente **1** — el anillo real de 3 empresas que sembramos
(`ratio_monto=1.06`) — y cero de las 20 empresas normales. Este filtro vive
adentro de la herramienta, así que protege tanto el triage de candidatos
como el veredicto final del verificador (ambos la usan).

### 9. El modelo seguía inventando identificadores a veces, pese a la instrucción

**Síntoma:** ~1 de cada 3 corridas, el Investigador citaba un identificador
que no existía — a veces un monto donde iba un `tx_id`, a veces un
placeholder tipo `TX_001`. El verificador lo rechazaba bien (nunca se coló
una acusación falsa), pero se perdían casos reales que sí tenían prueba.

**Diagnóstico:** pedirle "no inventes, copia literal" en el prompt no fue
suficiente — ya lo habíamos intentado. Es una limitación real del modelo
reproduciendo cadenas largas de memoria, igual que el bug de los montos.

**Fix estructural (no de prompt):** el código ahora rastrea, con una regex de
formato UUID, **todo identificador que apareció de verdad** en un resultado
de herramienta durante la conversación. Cuando el modelo entrega su JSON
final, cada identificador citado se valida contra ese conjunto. Si citó algo
que nunca vio, no se descarta de inmediato: se le regresa un mensaje con la
lista exacta de identificadores válidos y se le pide corregir (hasta 2
veces) antes de rendirse y descartar esa evidencia. Verificado con 3 corridas
consecutivas sobre los dos RFCs que más fallaban: 3/3 confirmados los dos,
con monto exacto, sin inventar nada.

### 10. Injerto de identidad real: el EFOS ya no es inventado

**Motivación:** hasta este punto, el RFC "EFOS" de la demo era 100% fabricado
con `gen_rfc()` — nada que un juez pudiera verificar contra una fuente
pública. El brief menciona explícitamente que el listado 69-B del SAT es un
dataset real y descargable.

**Qué se hizo:** se descargó el listado oficial completo de definitivos
(`omawww.sat.gob.mx/cifras_sat/Documents/Definitivos.csv`, 11,270 registros,
"información actualizada al 31 de diciembre de 2025"), se parseó con csv
real (el archivo trae nombres de empresa con comas dentro de comillas — un
split ingenuo por comas corrompe filas), y se filtró a 502 RFCs con
publicación en el DOF >= 2024, guardados en `sat_69b_reference.csv`
(rfc, razón social, fecha real de publicación).

**El detalle que importa — se invirtió la dirección de la fecha:**
`build_efos_scheme()` ya no *deriva* la fecha del DOF a partir de las
facturas inventadas (como antes: `dof_date = ultima_factura + 90-240 días`).
Ahora la fecha del DOF es el dato real y **dado**, y las facturas falsas se
generan hacia atrás desde ahí (`fecha_emision = dof_date - 90 a 400 días`) —
así se preserva la narrativa real del fraude: la víctima se dedujo el gasto
*antes* de que el SAT publicara que el proveedor era una EFOS.

**Verificado:** `check_sat_blacklist("MHR190316MM2")` devuelve
`situacion: DEFINITIVO`, `publicacion_dof: 2025-04-04` — datos reales,
verificables en el DOF — y el pipeline completo (triage → investigación →
verificación) corrió sin cambios, 4/6 confirmados, cero falsas
confirmaciones, igual que con el RFC inventado.

**Lo que sigue siendo sintético, honestamente:** `monto_presunto_total` (el
listado público del SAT no publica montos, solo identidad y fechas de
oficio), la fecha de constitución de la EFOS, y por supuesto toda la
víctima, sus facturas y sus pagos — el injerto es de identidad, no de
transacciones.

### 11. `check_sat_blacklist` solo "conocía" a un RFC real

**El hueco:** `sat_blacklist_69b.rfc` tenía `REFERENCES entities(rfc)` — una
FK a nuestra propia tabla de ~26 empresas sintéticas. Eso significa que la
herramienta que el modelo usa para preguntar "¿está este RFC en la lista
negra?" solo podía responder que sí para el **único** RFC que nosotros
elegimos a mano para la demo. Si el dataset del jurado trae facturas de
*cualquier otro* de los ~11,600 RFCs reales que están genuinamente en la
lista del SAT, el sistema habría dicho "no está en la lista" — falso, por
diseño, no por falla del detector.

**Fix:** se quitó la FK — la lista del SAT es un catálogo de referencia
externo, no debería depender de qué empresas decidimos auditar. Se cargó el
catálogo real completo (Definitivos + Presuntos, 11,631 RFCs deduplicados por
RFC, mismo origen oficial que la entrada 10) a la hoja `Lista_69B`, así que
`sat_blacklist_69b` ya no son ~26 filas sintéticas con 1 real metida a la
fuerza — son ~11,600 filas reales + los RFCs sintéticos del dataset.
Verificado: `check_sat_blacklist` reconoce correctamente 3 RFCs reales
elegidos al azar del catálogo (ninguno tocado a mano por nosotros), sigue
diciendo `false` para un RFC inventado, y el pipeline completo (triage →
investigación → verificación) corrió igual que antes — mismos 6 candidatos,
misma tasa de acierto.

### 12. `sat_blacklist_69b` se borraba con cada Excel nuevo

**El hueco:** `db.py` tenía un único `SCHEMA_SQL` que hacía `DROP TABLE` de
**todo**, catálogo del SAT incluido, cada vez que se llamaba `init_db()` --
que es justo lo que corre `ingest.py` en cada ingesta. Subir el Excel de una
empresa distinta habría borrado los 11,631 RFCs reales cargados en la
entrada 11, silenciosamente.

**Fix:** se separó el esquema en dos con ciclos de vida distintos:
- `sat_blacklist_69b` (catálogo de referencia del SAT) → `CREATE TABLE IF NOT
  EXISTS`, nunca se borra. Vive en `init_db()`.
- `entities/invoices/invoice_items/bank_ledger/investigation_cases` (los
  libros de la empresa que se está auditando AHORA) → se reemplazan por
  completo con cada Excel nuevo. Vive en la función nueva `reset_case_data()`.

**Verificado:** se generó un Excel de una "segunda empresa" con la hoja
`Lista_69B` vacía y se ingirió después del dataset original. Resultado:
`entities` pasó de 26 filas a 2 (las de la empresa nueva), pero
`sat_blacklist_69b` se mantuvo en 11,631 sin moverse, y
`check_sat_blacklist("MHR190316MM2")` siguió respondiendo correctamente
después del cambio.

### 13. Caso trampa: ¿el sistema calla ante un fraude que no sabe buscar?

**La pregunta que había que responder con evidencia, no con promesas:** si el
jurado trae un patrón de fraude que no es ninguno de nuestros 3 (EFOS,
kickback, sin materialidad), ¿el sistema inventa una acusación, o se queda
callado?

**El caso:** se agregó una empresa (`MRY1312186JL`) que recibe una
transferencia bancaria real de $847,961.06 **sin ninguna factura asociada**
(`cfdi_uuid = NULL`) — ingreso no declarado (Art. 59 CFF, "doble
contabilidad"/subdeclaración). A propósito, ninguno de los 4 detectores lo
cubre: no está en 69-B, no hay factura cuyo pago "no cuadre" porque no hay
factura en absoluto, no participa en ningún ciclo, no hay ningún concepto
que evaluar.

**Resultado 1 — el triage nunca lo investiga**, que es la defensa principal:
`get_candidate_rfcs()` no lo incluyó en ninguna corrida.

**Resultado 2 — hallazgo honesto al forzarlo:** se le pidió al Investigador
investigar ese RFC de forma directa, saltándose el triage a propósito, 3
veces. Las 3 veces terminó `DESCARTADO`, pero no porque el modelo haya dicho
limpiamente "no encontré nada" — trató de usar una factura **de otra
empresa** para construir un caso, la misma manía de inventar evidencia que
ya habíamos visto con `HCZ181008TJY` (entrada 6), solo que disfrazada
distinto. La verificación de "relevante" que ya existía (la evidencia debe
involucrar al RFC acusado) lo atrapó las 3 de 3 veces sin que hiciera falta
tocar nada nuevo — la misma defensa protegió contra un caso que no estaba
pensando en el momento de construirla.

**Conclusión de este momento (luego corregida en la entrada 14):** el
sistema no detecta subdeclaración de ingresos, y ante ese vacío nunca acusa
en falso — o el triage lo filtra antes de llegar al modelo, o el
verificador atrapa cualquier intento de inventar algo si de todos modos
llega.

### 14. Corrección: "subdeclaración de ingresos" no era indetectable — nos faltaba un detector

**El error en la entrada 13:** se trató la subdeclaración de ingresos como
si fuera tan indetectable como la doble contabilidad. No lo es. Doble
contabilidad de verdad necesita dos juegos de libros para comparar, algo que
nuestro esquema no tiene. Pero "dinero que entra por banco sin ninguna
factura" es el espejo exacto del detector 2 que YA existía ("factura sin
pago que la respalde") — nomás en la otra dirección. El brief mismo lo
describe como un solo detector bidireccional: *"payments that do not match
invoices"*. Un pago que no tiene NINGUNA factura es el caso más extremo de
"no coincide con una factura" — no un tipo de fraude ajeno al sistema.

**Fix:** se agregó como una 5ª tipología completa, no un parche:
- `INGRESO_NO_DECLARADO` en el `CHECK` de `investigation_cases` (`db.py`).
- Detector 5 en `get_candidate_rfcs()`: `bank_ledger` con `cfdi_uuid IS NULL`
  y monto >= $200,000, agrupado por `cuenta_destino_rfc`.
- `_verify_ingreso_no_declarado()` en `verifier.py`: confirma que la
  transferencia citada de verdad la recibió el acusado y de verdad no tiene
  CFDI asociado.
- Nueva entrada en la prioridad de tipologías del Investigador (3er lugar,
  entre KICKBACK_CIRCULAR y EMPRESA_FACHADA — es un hecho bancario objetivo,
  no una inferencia de texto como SIN_MATERIALIDAD).

**Verificado:** el mismo RFC de la entrada 13 (`MRY1312186JL`) ahora aparece
en el triage y se confirma con el monto exacto ($847,961.06) en 6 de 6
corridas (3 investigaciones forzadas + 3 corridas completas del pipeline).

**La lección real de esto:** antes de decidir que un patrón "no se puede
detectar", hay que preguntar si de verdad requiere datos que no tenemos
(como doble contabilidad) o si solo nos faltó construir el detector
obvio, usando datos que ya teníamos desde el principio.

### 15. Visión: lee el dinero perfecto, pero rompe los identificadores

**La prueba:** se le dio a `qwen3.5:4b` la imagen de un CFDI real (un recibo de
nómina timbrado) y se le pidió extraer los campos, con `temperature=0`. La
verdad de referencia se puede leer a simple vista en la imagen, así que el
error se mide exacto.

| Campo | Real | Leyó | |
|---|---|---|---|
| folio fiscal (UUID, 36 caracteres) | `F545A134-664F-4047-BC06-C93AB2A0D2CA` | igual | correcto |
| subtotal | 7,464.57 | 7,464.57 | correcto |
| descuento | 620.62 | 620.62 | correcto |
| total | 6,843.95 | 6,843.95 | correcto |
| razón social | ALMACEN DE DROGAS | igual | correcto |
| RFC receptor | HECD0511139P3 | igual | correcto |
| **RFC emisor** | **ADR531130N5A** | **ADSR51130N5A** | **MAL** |
| método de pago | PUE | PU | truncado |

**El error del RFC es 100% reproducible:** 3 de 3 intentos devolvieron
exactamente el mismo `ADSR51130N5A` — insertó una S y perdió un 3. No es ruido
aleatorio, es una falla sistemática de lectura.

**Por qué esto es lo peor que podía fallar:** todo el día el diseño se apoyó en
"el modelo señala identificadores, el código resuelve los valores". Aquí pasó
justo al revés de lo esperado — los montos salieron perfectos y el
**identificador** se rompió. Y el RFC es precisamente la llave con la que se
consulta la lista 69-B: un RFC mal leído hace que `check_sat_blacklist`
responda "no está en la lista" sobre una empresa que no existe. Es un falso
negativo **silencioso**, que se ve idéntico a una revisión limpia.

**Conclusión:** la lectura por visión no puede alimentar la base sin
confirmación humana de los identificadores. Los montos sí se pueden confiar;
los RFCs y folios, no.

### 16. El modelo inventaba el concepto que le pasaba a la herramienta

**Síntoma:** `QXT121125K07` (la empresa fachada sembrada, un fraude real)
regresaba `tipo_esquema` vacío — el modelo concluía "no hay nada". No era falta
de turnos: concluía en 4 de 20.

**Diagnóstico:** el rastreo de argumentos mostró que llamó
`verify_service_materiality(rfc="QXT121125K07", concepto="Servicios de
consultoría en tecnología y desarrollo de software")` — un concepto
**inventado**. El real en la base es *"Consultoría estratégica en fusiones y
adquisiciones corporativas"*. Nunca consultó `invoice_items`. Y como el
concepto inventado sí describe un entregable concreto, pasó la prueba de
materialidad y el fraude se declaró inexistente.

Es el mismo bug de la entrada 5, que en su momento se intentó arreglar con una
instrucción en el prompt ("no lo inventes, tráelo con query_database"). La
instrucción no aguantó.

**Fix estructural:** la herramienta cambió de firma. Ya no recibe
`concepto` (texto libre que el modelo puede inventar) sino `invoice_uuid`, y
lee los conceptos reales de la base ella misma. Mismo principio que con los
montos y la razón social: el modelo señala QUÉ fila revisar, el código resuelve
su contenido.

**Verificado:** la herramienta devuelve `match=False` sobre el uuid real,
`match=None` sobre un uuid inventado, y en la corrida completa `QXT121125K07`
pasó de fallar a confirmar `SIN_MATERIALIDAD` con $949,764.83 exactos.

### 17. El RFC mal leído ya no pasa en silencio (revisa la conclusión de la 15)

La entrada 15 cerró con que la visión no puede alimentar la base sin
confirmación humana de los identificadores. Eso resolvía el riesgo pero mataba
la demo: obliga a que un humano teclee cada RFC. La pregunta correcta no era
"¿cómo hago que el modelo lea mejor?" sino **"¿cómo detecto con código que leyó
mal?"** — el mismo principio que gobierna todo lo demás aquí.

**La clave que se nos había pasado:** el último carácter de un RFC no es un dato
más, es un **dígito verificador** calculado a partir de los otros once. Se
comprueba con aritmética, sin catálogo y sin red. Ya estaba ahí desde el
principio, dentro del propio dato.

`core/rfc.py` implementa dos capas independientes:

1. **Dígito verificador** (`rfc_valido`) — aritmética pura sobre el RFC.
2. **Vecino más cercano** (`sugerir_rfc`) — distancia de edición ≤ 2 contra el
   universo conocido: las 27 entidades del caso + los 11,631 RFCs reales del
   listado 69-B que ya teníamos cargados.

**Medición contra los 11,631 RFCs reales del SAT:**

```
pasan el dígito verificador : 11,606  (99.79%)
no pasan                    :     25  (0.21%)
```

Errores de un carácter simulados sobre RFCs válidos: **detecta el 84.65%.**

**Sobre el caso real de la entrada 15:**

| RFC | checksum | conocido | veredicto |
|---|---|---|---|
| `ADSR51130N5A` (lo que leyó Qwen) | **falla** | no | no confiable — "debería terminar en 8, no en A" |
| `ADR531130N5A` (el real) | pasa | no | confiable |

**Las dos capas no son redundantes.** En la prueba se corrompió un RFC real del
69-B (`AAA120730823` → `AAAX20730823`) y el error **sí pasó el checksum por
casualidad** — dentro del 15% que se le escapa. La búsqueda por cercanía lo
cachó igual y sugirió el RFC correcto. Cada capa agarra lo que a la otra se le
va. Costo: 0.066s por RFC contra el universo completo.

**Por qué el 0.21% obliga a un diseño específico:** 25 RFCs publicados por el
propio SAT no pasan su propio dígito verificador. Por eso `revisar_rfc_leido`
**nunca descarta** un RFC por su cuenta: devuelve `confiable=False` y lo manda a
revisión. Rechazar automáticamente convertiría un error de lectura en algo
peor — perder una empresa que sí está realmente listada.

**Resultado:** el falso negativo silencioso deja de ser silencioso. El sistema
ya no contesta "no está en la lista" sobre un RFC que nunca existió; contesta
"este RFC no cuadra, ¿quisiste decir `ADR531130N5A`?". La confirmación humana
pasa de ser obligatoria en cada campo a ser la excepción, solo donde el código
levantó la mano.

### 18. El mismo CFDI en PDF y en foto: el experimento controlado

Hasta aquí el sistema solo comía Excel. Una búsqueda en los 15 archivos Python
del proyecto confirmó que la única mención de "PDF" o "imagen" era un
comentario: si un juez llegaba con un PNG, el programa no leía mal — **ni
siquiera arrancaba** (`ValueError: cargar_tabular solo acepta .xlsx/.xls/.csv`).

Se consiguió el **mismo comprobante** en los dos formatos: el PDF timbrado y una
foto de su representación impresa. Mismo UUID, mismos montos, misma empresa. Eso
permite comparar sin nada más de por medio:

| | RFC del emisor | veredicto del validador |
|---|---|---|
| PDF (capa de texto, sin modelo) | `ADR531130N5A` | correcto |
| PNG (visión, `qwen3.5:4b`) | `ADSR51130N5A` | **REVISAR** — "debería terminar en 8" |

**Conclusión de diseño:** si el documento trae texto, el modelo no lo toca. La
visión es el último recurso, no el primero. `cargar_documento` intenta en este
orden: capa de texto del PDF → rasterizar → visión. Del PDF, los campos salen
por expresión regular: exacto, gratis, sin posibilidad de alucinación.

**Hallazgo secundario — pedir más campos hace que el modelo abandone campos.**
Pidiéndole 4 campos a la imagen, devolvió los dos RFC. Pidiéndole los mismos
campos dentro de una lista de 15, devolvió un JSON completo y bien formado pero
con `receptor_rfc`, `razon_social_receptor` y los dos códigos postales
**vacíos** — 2 de 2 intentos, y sin truncarse (374 de 1024 tokens disponibles).
No era presupuesto ni formato: era atención repartida. La lectura se partió en
**tres pasadas cortas** (identificadores / montos / descriptivos) y el
`receptor_rfc` volvió a salir correcto.

**El PAC no es parte de la operación.** El texto del CFDI contiene *tres* RFC,
no dos: el tercero (`SCD110105654`) es el proveedor autorizado que timbra el
comprobante. Tomarlo a ciegas habría inventado una relación comercial que no
existe, así que se excluye explícitamente por su etiqueta y se reporta que se
ignoró.

**`fecha_constitucion` pasó a aceptar nulos.** Un CFDI identifica al emisor y al
receptor pero no dice cuándo se constituyó la empresa. Con la columna en `NOT
NULL`, la única forma de ingerir un documento era inventar una fecha — justo lo
que este sistema existe para no hacer. Se verificó con `grep` en todo el
proyecto que **ningún detector lee ese campo** (`EMPRESA_FACHADA` lo decide el
Investigador por prompt, no por esa fecha), así que dejarlo vacío no degrada
ninguna detección. Se prefirió un hueco honesto a un dato fabricado.

**Una sola puerta.** `universal_loader.cargar()` despacha por extensión y el
resto del pipeline nunca se entera de en qué formato llegó el caso. Un Excel que
ya viene canónico se ingiere tal cual, sin pasar por el traductor: es la ruta
probada y meterle un paso de más solo agregaría dónde romperse.

### 19. El pipeline completo, de punta a punta, con el Auditor encendido

Hasta esta entrada, `auditor.py` (Gemini) nunca se había ejecutado: la bitácora
cubría las fases 0-2. Correrlo destapó tres cosas, y solo una era del código
original.

**Lo que funcionó a la primera.** Las fases 1-3 salieron completas y estables:

```
CONFIRMADO  KZH161209V32  EFOS_69B              $2,245,374.00
DESCARTADO  KZR170308INT  (sin evidencia referenciada)
CONFIRMADO  LSM120111199  KICKBACK_CIRCULAR     $1,275,488.33
CONFIRMADO  MRY1312186JL  INGRESO_NO_DECLARADO  $  847,961.06
CONFIRMADO  MXZ220310ALK  KICKBACK_CIRCULAR     $  627,727.24
CONFIRMADO  QXT121125K07  SIN_MATERIALIDAD      $  949,764.83
CONFIRMADO  VRH22081728I  KICKBACK_CIRCULAR     $1,315,556.03

6 confirmados · 1 descartado · $7,261,871.49 · 3 corridas idénticas · 45-70s
```

Las **cinco** tipologías aparecen en una sola corrida, y el único descarte es
correcto: un lead sin evidencia que el verificador tumbó.

**Problema 1 — una caída de Gemini tiraba toda la investigación.** La primera
corrida murió con `503 UNAVAILABLE` ("high demand"), un error del servidor de
Google, no del código. Pero el pipeline entero se cayó con él y se perdió el
trabajo de las tres fases previas. Para una demostración en vivo ese es el peor
modo de falla posible, y el más fácil de evitar. Tres cambios:

1. **Reintento con espera creciente** — un 503/429 es transitorio por
   definición. En una corrida posterior el intento 1 recibió 503 y el intento 2
   funcionó.
2. **Redacción de respaldo sin ningún LLM** (`redactar_sin_modelo`). El Auditor
   solo REDACTA: los montos, RFC, UUID y tipologías ya vienen verificados
   contra la base. Un expediente sin Gemini no es un expediente con menos
   pruebas — es el mismo hecho con peor prosa. Se probó aislado y el documento
   sale completo, con su tabla de cadena de evidencia.
3. **Persistir antes de la llamada de red.** `persist_cases` corría *después* de
   Gemini, así que la corrida fallida no guardó nada pese a tener el dictamen ya
   decidido. Se verificó el arreglo: con Gemini caído, los 7 dictámenes quedaron
   en `investigation_cases`.

**Problema 2 — los defaults de modelo estaban rotos.** Medido contra la API:
`gemini-2.5-pro` (el default de `config.py`) devuelve **429, cuota agotada**, y
`gemini-2.5-flash` devuelve **404, ya no disponible**. El proyecto solo
funcionaba porque el `.env` local los pisaba: quien clonara el repo se quedaba
con un default muerto. Corregido en `config.py` y en `.env.example`.

**Problema 3 — un bug introducido al arreglar el problema 1.** Los reintentos se
escribieron como `_client().models.generate_content(...)`, dejando al `Client`
como temporal sin ninguna referencia viva: Python lo recolectaba y cerraba su
sesión HTTP a media llamada. Los tres intentos fallaron con *"the client has
been closed"* con la API perfectamente disponible. Se guarda en una variable.

**Verificación de la promesa central.** Se cruzó el expediente redactado por
Gemini contra `fraud.db`, campo por campo:

| Prueba | Resultado |
|---|---|
| Montos de los 6 casos presentes | 6/6 |
| Identificadores citados que existen | 10/10 (6 facturas + 4 transferencias) |
| Cifras citadas que no existen en la base | **0** |

Vale registrar que las dos primeras versiones de esa verificación dieron falsas
alarmas **por errores del verificador, no del expediente**: una comparaba montos
como texto (`$2245374.0` contra `2,245,374.00`: mismo número, distinto formato)
y la otra buscaba los identificadores solo en `invoices.uuid`, ignorando
`bank_ledger.tx_id`, que en esta base también tiene forma de UUID. La lección es
la de siempre en este proyecto: una alarma no es un hallazgo hasta que se
descarta que el error esté en quien mide.

**Un último detalle de formato, con causa nuestra.** El expediente salía con
`$2245374.0`: sin separadores y con un decimal. El Auditor no se equivocó —
copió fielmente lo que le mandamos, porque `_format_confirmados` interpolaba el
float de Python crudo. Ahora el monto se formatea como moneda *antes* de entrar
al prompt, para que el modelo solo tenga que copiar. Copiar es más seguro que
reformatear.

### 20. El frontend: Streamlit, y un orquestador que ya no vive solo en `main.py`

El equipo no tiene a nadie con experiencia en programación web, así que se
descartó cualquier opción con HTML/CSS/JS (FastAPI+React, FastAPI+HTML/JS). Se
eligió **Streamlit**: el frontend completo (`app.py`) está escrito en Python
puro, sin una sola línea de otro lenguaje.

**El problema real no era "qué framework", era "cómo mostrar 4 fases que tardan
45-70s sin que la pantalla se quede en blanco hasta el final".** `main.py`
corría las 4 fases de un jalón y solo imprimía al terminar cada una — servía
para terminal, pero un frontend necesita actualizarse mientras el pipeline
avanza, no solo al final.

**Solución: separar el orquestador de cómo se muestra.** `core/pipeline.py`
tiene ahora la única copia de "qué hace el pipeline y en qué orden", como un
generador (`ejecutar_pipeline`) que va entregando eventos
(`{"fase", "estado", ...}`) conforme ocurren — un RFC más investigado, un caso
confirmado, el expediente terminado. `main.py` quedó reducido a imprimir esos
eventos en terminal; `app.py` los consume igual y va llenando la página en
vivo. Ninguno de los dos reimplementa la lógica del pipeline — ambos son
"vistas" del mismo generador. Es el mismo principio que ya resolvió `config.py`
con los umbrales duplicados: una sola fuente de verdad, no dos copias que se
puedan desincronizar.

**Verificado:** tras la refactorización, `main.py` se corrió contra el dataset
completo y produjo la misma secuencia de 4 fases con el mismo formato de
salida que antes del cambio (la diferencia en cuántos RFC confirmó una corrida
frente a otra es la variabilidad ya conocida de Qwen entre corridas — ver
"Estado actual" — no algo introducido por la refactorización). `app.py` se
arrancó en modo headless y respondió `HTTP 200` sin errores de importación.

### 21. Expediente en PDF, historial que sobrevive, y cuatro páginas

Tres cosas que resolver de golpe: que la interfaz no se viera "de hackathon",
que hubiera un PDF profesional descargable, y que las corridas anteriores no se
perdieran.

**El PDF no se arma leyendo el Markdown de Gemini.** Se arma desde los leads ya
verificados — los mismos diccionarios que salieron de `verifier.py` con cada
monto resuelto por SQL. De Gemini se toma solo la prosa narrativa. Las razones
son dos y las dos pesan:

- *Corrección.* Si el PDF se armara parseando el texto del modelo, cualquier
  error suyo de formato se volvería un error en el documento firmado. Con los
  datos estructurados, los montos y UUID del PDF vienen del mismo lugar que ya
  se verificó contra la base.
- *Consistencia.* Un LLM formatea distinto en cada corrida (a veces `###`, a
  veces una tabla, a veces una lista). El documento sale idéntico siempre, y da
  exactamente igual si lo redactó Gemini o la plantilla de respaldo.

Verificado sobre un expediente de prueba: 5 páginas, portada con folio y
recuadro de cifras, tabla resumen, una sección por caso con su cadena de
evidencia, sección de descartados, numeración de página, y las 9
comprobaciones de contenido (montos, UUID, acentos) en verde. Usa `fpdf2`
porque es Python puro: `weasyprint` y `pdfkit` exigen binarios del sistema
(GTK, wkhtmltopdf) que en Windows son una fuente de fallas justo el día de la
demostración. La fuente se toma de las TTF del sistema, con degradación a la
fuente interna si el repo se clona en otro sistema operativo.

**El historial: un tercer ciclo de vida.** `investigation_cases` vive dentro de
`SCHEMA_CASO_SQL`, así que **se borraba entera con cada ingesta** — cada corrida
destruía los dictámenes de la anterior. La solución reusa el patrón que ya
existía (entrada 12): la nueva tabla `expedientes_historial` vive en el esquema
de REFERENCIA, el mismo que protege al catálogo del SAT y que nunca se borra.
Dos decisiones deliberadas dentro de ella:

1. **Sin llave foránea a `entities(rfc)`.** Las entidades del caso desaparecen
   en la siguiente ingesta; una FK volvería imposible conservar el expediente de
   una empresa auditada la semana pasada. Es el mismo motivo por el que
   `sat_blacklist_69b.rfc` tampoco la tiene.
2. **`payload_json` guarda los leads verificados completos.** Sin ese campo el
   historial sería un recibo sin contenido; con él, el PDF se reimprime meses
   después aunque los datos originales ya no estén en la base.

Probado explícitamente: se guardó un expediente, se corrió `reset_case_data()`,
y el expediente seguía ahí y se reimprimió en PDF sin pérdida de montos.

**El Auditor ahora reporta quién redactó.** `generate_case_file` devuelve
`(markdown, 'gemini' | 'plantilla')`. No es cosmético: el expediente se archiva,
y hay que poder decir meses después si esa redacción salió del modelo o del
respaldo. Ocultarlo haría que un documento redactado sin modelo se viera
idéntico a uno redactado con él.

**Cuatro páginas, y una regla para la de Configuración.** Investigación,
Expedientes, Catálogo 69-B (con un validador de RFC en vivo que enseña el
dígito verificador atrapando un error de lectura) y Configuración. En esta
última, *solo se muestra como control lo que de verdad hace algo*: el modelo y
el archivado son controles reales; los umbrales de detección se muestran en
modo lectura porque hoy se leen de `config.py` al importar. Ponerles un
deslizador que no afecta nada sería justo la clase de fachada que este proyecto
existe para no construir.

**Detalle operativo que se repite: hay que reiniciar el servidor tras tocar
`core/`.** Streamlit recarga los *scripts* cuando cambian (`app.py`, las
páginas), pero **no reimporta los módulos** que ya tiene en `sys.modules`. Si se
edita `core/pipeline.py` con el servidor encendido, la interfaz sigue llamando a
la versión vieja y aparecen errores que no corresponden al código en disco. El
caso real que costó tiempo:

```
TypeError: ejecutar_pipeline() got an unexpected keyword argument 'guardar_historial'
```

…con el parámetro claramente presente en el archivo, y un `import` en un proceso
nuevo aceptándolo sin problema. No era un bug del proyecto y no se arreglaba
editando nada: el servidor llevaba encendido desde antes del cambio. Se mata
(`Ctrl+C`) y se vuelve a levantar. Conviene recordarlo antes de perder tiempo
depurando un fantasma.

### 22. El interrogatorio: cuando inventar un dato no era el único riesgo

El brief pide esto dos veces — en qué construir (*"responde una pregunta
sorpresa sobre su razonamiento"*) y en el criterio de Judgment (*"¿puede
defender un hallazgo cuando un juez pregunta?"*). Era el único momento en que el
jurado interactúa directamente con el agente, y no existía nada.

`agents/defensor.py` reusa el mismo loop ReAct y las mismas cuatro herramientas
de solo lectura del Investigador, con una diferencia de fondo: **aquí el modelo
no decide nada.** El dictamen ya lo cerró `verifier.py`; el Defensor solo explica
decisiones tomadas, y puede ir a la base a traer la fila exacta que las respalda.

**El hallazgo que obligó a rediseñar la guarda.** La primera versión heredó del
Investigador la protección contra identificadores inventados (bitácora 9): se
recolecta todo UUID y RFC que el modelo realmente vio, y si cita uno que no
existe, se le corrige. En la primera prueba real esa guarda no detectó nada… y
la respuesta era igual de inaceptable:

```
Pregunta:  ¿Por qué acusaste a esta empresa?
Respuesta: "No acusé a GDH210804LCC porque su proveedor no aparece en el
            listado 69-B..."
```

El expediente daba ese caso por **CONFIRMADO**. El modelo no inventó ningún
dato: **invirtió la conclusión**. La lección es que proteger los identificadores
no basta — el razonamiento también se puede fabricar, y un agente que se desdice
de su propia acusación frente a un auditor hace más daño que uno que admite no
saber.

Dos respuestas, porque una sola no alcanzaba:

1. **Regla 0 en el prompt: el dictamen no se relitiga.** Nunca decir que no se
   acusó a alguien que está en confirmados, ni al revés. Y si al consultar la
   base encuentra algo que parece contradecir el expediente, decirlo explícito
   ("el expediente confirma X, pero el registro Y muestra Z") en vez de cambiar
   la conclusión en silencio. Señalar una inconsistencia es útil; desdecirse
   calladamente destruye la credibilidad del expediente completo.
2. **Un detector de contradicción en código** (`_contradice_expediente`): busca
   una negación en los ~140 caracteres previos a la mención de un RFC
   confirmado. Es una heurística de texto, no un juez semántico, así que
   **advierte en vez de bloquear** — igual que el validador de RFC nunca
   descarta solo. 4 de 4 en pruebas unitarias, incluidos los dos casos negativos
   que no deben disparar.

**Diagnóstico honesto de la falla original:** el caso de prueba que le di era
internamente inconsistente — afirmaba `EFOS_69B` sobre un proveedor que no
estaba en el listado. Repetida la prueba con un caso real de la base
(`KZH161209V32` que sí compró a `MHR190316MM2`, DEFINITIVO en el 69-B), las tres
preguntas salieron limpias en 3.5–4.4s, citando el UUID real y el monto exacto,
y consultando la base hasta 4 veces para citar la razón registrada de un
descarte tal cual quedó asentada. O sea: el modelo tenía parte de razón al
objetar. La guarda se queda igual, porque el día de la demostración nadie
garantiza que la entrada sea consistente.

### El rastro del dinero

Clarity pide *"un rastro claro del dinero"* y "qué construir" pide que *"el
agente rastree el dinero en pantalla"*. Había tablas, no recorrido.

`core/money_trail.py` arma un diagrama en formato DOT desde la evidencia ya
verificada y lo entrega como texto; **`st.graphviz_chart` lo dibuja en el
navegador**. Esto es deliberado: ni el paquete de Python `graphviz` ni el binario
`dot` del sistema hacen falta. Esta máquina sí tiene `dot.exe`, pero la del
salón puede que no, y un diagrama que no aparece el día de la presentación vale
menos que no tenerlo. Mismo criterio que llevó a `fpdf2` sobre `weasyprint`.

**Un cambio necesario en `enrich_lead`:** la evidencia enriquecida guardaba el
monto y el identificador de cada factura y transferencia, pero **no las
contrapartes** — sabía *cuánto* se movió, no *entre quiénes*. `_get_invoice` y
`_get_bank_tx` ya las traían de la base; solo no se almacenaban. Ahora sí, con lo
que el expediente guardado se vuelve auto-suficiente para dibujar el flujo.

Para `KICKBACK_CIRCULAR` hay un paso extra: la evidencia citada suele traer uno
o dos tramos, y un tramo suelto no se ve como fraude — el anillo completo sí. Se
reconstruye desde `bank_ledger` y se dibuja en rojo; si esos datos ya no están
(expediente viejo), cae de vuelta a las aristas de la evidencia guardada.

**La interfaz tuvo que reestructurarse para esto.** Streamlit reejecuta el script
completo con cada interacción, así que escribir una pregunta en el interrogatorio
disparaba otra investigación de 60 segundos y perdía el expediente anterior. Los
resultados ahora se guardan en `st.session_state` en cuanto el pipeline termina,
y el PDF se construye una sola vez ahí mismo en vez de rehacerse con cada
pregunta.

Ninguna de las dos piezas agregó una sola dependencia nueva.

### 23. El sistema era ciego a las empresas chicas

La pregunta que lo destapó: *"si le meto casos nuevos, ¿el modelo realmente sabe
identificar el truco, o solo funciona con los datos que ya probamos?"*. El
criterio #1 del brief es justamente ese — *"en registros que nunca ha visto"* —
y hasta aquí todo se había probado contra el mismo dataset que nosotros
generamos.

**La medición.** Se sembró la misma empresa con los mismos tres fraudes (EFOS,
anillo de dinero, depósito sin factura) a tres escalas de monto, y se midió si
el TRIAGE los nominaba. El triage es la puerta: si no nomina al RFC culpable, el
modelo nunca lo investiga. Resultado con los umbrales absolutos originales:

```
escala 1.00  (anillo $805,807 · depósito $907,986)  ->  3 de 3
escala 0.20  (anillo $168,702 · depósito $215,597)  ->  3 de 3
escala 0.05  (anillo  $34,468 · depósito  $50,989)  ->  1 de 3
```

A escala chica el triage nominó **un solo RFC**. Sobrevivió únicamente el
detector del listado 69-B, que es un `JOIN` y no compara montos; el anillo y el
depósito quedaron debajo de `MIN_CICLO_MONTO` ($50,000) y
`MIN_INGRESO_SIN_FACTURA` ($200,000). Una pantalla casi vacía, indistinguible de
"aquí no hay fraude": el falso negativo silencioso otra vez, ahora en el triage.

Y un detalle que conviene no pasar por alto: a escala 0.20 el depósito pasó por
**7% de margen** ($215,597 contra $200,000). Estaba pasando de panzazo, no por
diseño.

**La causa de fondo** es que "un monto grande" no significa lo mismo para un
corporativo que para una PyME. Los umbrales ahora son **percentiles de los
movimientos reales de la empresa auditada** (`tools.umbral_monto_movimientos`),
con un piso absoluto chico para el caso degenerado de poquísimos movimientos.
Medición después del cambio:

```
escala 1.00  ->  3 de 3   (umbral de ciclo calculado: $220,659)
escala 0.20  ->  3 de 3   (umbral de ciclo calculado:  $44,805)
escala 0.05  ->  3 de 3   (umbral de ciclo calculado:  $14,529)
```

El umbral se mueve solo por un factor de ~15 entre escalas. Y la prueba de
regresión importaba tanto como la de cobertura: **sobre el dataset de
demostración el triage sigue nominando exactamente los mismos 7 RFC**. Aflojar
un umbral es fácil; aflojarlo sin inundar el sistema de falsos positivos es el
punto.

**El acoplamiento que había que respetar.** `MIN_INGRESO_SIN_FACTURA` lo usaban
DOS módulos: el triage para nominar y `verifier._verify_ingreso_no_declarado`
para confirmar. Cambiar solo el triage habría hecho que el sistema seleccionara
casos que él mismo descarta después. Por eso ambos llaman ahora a la misma
función `tools.umbral_ingreso_sin_factura()` en vez de leer una constante cada
uno — la misma lección que originó `config.py`, aplicada a un umbral que ya no
es una constante sino un cálculo.

**Para datos propios** se agregó `plantilla_entrada.xlsx`: las 5 hojas
canónicas con una fila de ejemplo y una hoja de instrucciones que dice qué
columnas son obligatorias. La hoja `Lista_69B` puede ir vacía — los 11,631 RFC
reales del SAT ya viven en la base y no se borran con las ingestas.

### 24. Punta a punta con datos nunca vistos: tres fallas que solo salen así

La entrada 23 midió el TRIAGE a distintas escalas. Faltaba lo otro: correr el
pipeline COMPLETO sobre una empresa fabricada con otra semilla, otra escala
(0.31, que no era ninguna de las probadas), otros RFC y un proveedor 69-B real
elegido al azar — y después interrogar al Defensor sobre ESE caso. Es la
simulación de lo que pasa en la demostración.

**El resultado principal, que era la pregunta de fondo del proyecto:**

```
sembrado EFOS_69B              CDT2003026R7  ->  CONFIRMADO  $151,879.20
sembrado KICKBACK_CIRCULAR     WSJ220919GY3  ->  CONFIRMADO  $361,183.96
sembrado INGRESO_NO_DECLARADO  ZDX1512040SV  ->  CONFIRMADO  $170,310.88

3 de 3 encontrados Y probados · 36.2s · Gemini caído, expediente entregado igual
```

Pero la corrida destapó tres fallas que el dataset de demostración nunca habría
mostrado.

**Falla 1 — se acusaba de lavado a empresas que solo se venden entre sí.** De 7
casos confirmados, 2 eran un par recíproco (`FKX1808265N9 ⇄ QJP210912JVL`,
ratio 1.119, $99,721 y $111,612) salido de las transferencias NORMALES del
generador. `find_money_cycles` aceptaba ciclos de 2 nodos, y "A me paga y yo le
pago" es el patrón de comercio inocente más común que existe. El round-tripping
real necesita al menos un intermediario para disfrazar el origen del dinero —
eso es su definición, no una heurística. Se exige `MIN_NODOS_CICLO = 3`.

En el dataset de demostración esto nunca había aparecido, pero por **suerte
estadística**: con 20 empresas y 50 transferencias aleatorias, un par recíproco
con montos parecidos es poco probable, no imposible. Es exactamente el tipo de
falso positivo que golpea el criterio de Judgment del brief — *"¿se niega a
acusar a proveedores que no puede respaldar?"*.

Tras el arreglo: los 3 fraudes sembrados siguen encontrándose, el par inocente
desaparece (de 7 confirmados a 5), y en el dataset de demostración el triage
sigue nominando **los mismos 7 RFC** con su anillo de 3 nodos intacto.

**Falla 2 — una llave faltante tumbaba el pipeline entero.** `_client()` hacía
`os.environ["GEMINI_API_KEY"]` y se llamaba FUERA del bloque de reintentos, así
que un `KeyError` mataba la fase 4 en vez de caer a la plantilla. Cualquiera que
clonara el repo sin `.env` se topaba con eso — el mismo modo de falla de la
bitácora 19, por una causa distinta. Verificado: sin llave, el expediente se
genera igual y **conserva el monto exacto y el RFC**.

**Falla 3 — mis propias guardas marcaban en rojo respuestas correctas.** Al
interrogar al Defensor sobre este caso nuevo aparecieron dos alertas falsas:

- `_contradice_expediente` marcó *"en esta corrida no hubo leads descartados; el
  expediente solo imputó a KZH161209V32"* como contradicción. La negación estaba
  en la oración ANTERIOR, del otro lado del punto y coma. Ahora la ventana se
  recorta en `.`, `;` y `:`.
- La guarda de identificadores marcó como inventado un RFC que **el auditor
  había escrito en su propia pregunta**, y que el modelo citó de vuelta para
  decir correctamente que no existe en la base. Ahora los identificadores de la
  pregunta se siembran como "vistos".

Una alerta roja sobre una respuesta buena es **peor que no tener alerta**:
frente a un auditor desacredita justo lo que sí se sostiene. 8 de 8 en pruebas
unitarias tras el recorte, conservando los tres casos de negación real.

**Falla 4, de contenido — el Defensor inventaba exoneraciones.** Con la lista de
descartados vacía, a la pregunta *"¿por qué no acusaste a las demás?"* fabricaba
razones legales para empresas que nunca fueron leads, y llegó a decir que un
proveedor era *"solo presunta"* cuando su propia respuesta anterior lo había
declarado DEFINITIVO. Dos medidas: una regla que obliga a responder "los
detectores no la señalaron como pista" en vez de inventar, y
`_contradice_situacion_69b`, que contrasta contra la base la situación que el
texto afirma (5 de 5 en pruebas unitarias). La distinción no es cosmética:
sobre un PRESUNTO la imputación de EFOS **no se sostiene**.

Verificado después: las tres preguntas salen limpias, admitiendo que no hubo
descartados y citando el UUID real con el monto exacto.

### 25. Dos de los cinco esquemas estaban muertos en datos de un tercero

Buscando qué más podía fallar con datos ajenos apareció un hueco silencioso.
`verify_service_materiality` leía el giro de cada empresa de
`giro_catalog.json`, un archivo que **solo escribe nuestro propio generador de
datos**. Medido sobre un dataset de terceros con una empresa fachada sembrada:

```
giro_catalog.json conoce 26 RFC (los del demo), no al sembrado
el triage SÍ lo nominó  (detector 4, coincidencia de texto)
verify_service_materiality -> match = None
    "El RFC no tiene un giro registrado en el catálogo"
```

Como `_verify_materialidad` exige `match is False` para confirmar, `None` nunca
confirma: **`SIN_MATERIALIDAD` y `EMPRESA_FACHADA` eran imposibles de probar en
cualquier dataset que no fuera el nuestro.** Y el daño era doble, porque el
triage sí nominaba: el sistema gastaba una investigación completa del modelo en
un caso que estaba condenado a descartar de antemano.

**El arreglo: el giro sale de los propios libros.** Lo que una empresa factura
dice a qué se dedica. Si todas sus facturas como emisor dicen "servicios de
transporte de carga", ese es su giro — no hace falta un catálogo externo para
saberlo. `_descripciones_que_emite` lo lee de `invoice_items`, y el catálogo se
conserva como fuente preferente para no cambiar el comportamiento del dataset de
demostración. Además es más defendible ante un auditor: *su propio historial
dice a qué se dedica*.

**Medición después del cambio:**

```
DATOS AJENOS (sin catálogo)
  emite: "Servicios de transporte de carga terrestre"
  recibe: "Consultoría estratégica en fusiones y adquisiciones" por $780,000
  -> match=False -> CONFIRMADO_CON_PRUEBA $780,000.00     (antes: imposible)

DEMOSTRACIÓN (con catálogo)
  QXT121125K07 -> match=False -> CONFIRMADO $949,764.83   (idéntico a antes)

SIN HISTORIAL DE EMISIÓN
  -> match=None, "no hay con qué contrastar el concepto"  (se abstiene, correcto)
```

**Lo que importaba conservar.** En la misma corrida, dos empresas que reciben
*"Consultoría en sistemas de información"* siguen dando `match=True`: el
concepto es ajeno a su giro pero **describe un servicio específico y
verificable**, así que no se marca. Esa discriminación costó trabajo construirla
(entrada 5) y el arreglo no la volvió un gatillo fácil — aflojar la cobertura
sin aflojar el estándar de prueba era exactamente el punto.

### El sistema se calla cuando no hay nada

En la misma tanda se probó algo que nunca se había medido: **libros limpios**,
sin ningún fraude sembrado. El triage nominó **0 RFC**. No hubo investigación,
no hubo acusaciones, no hubo narrativa inventada para justificar el esfuerzo.

Es un resultado corto pero vale registrarlo: un detector que siempre encuentra
algo no sirve de nada. Que el sistema no diga nada cuando no hay nada que decir
es la otra mitad de *"probar antes de acusar"*.

---

## Estado actual (verificado, no aspiracional)

Pipeline completo, incluido el Auditor (Gemini), corrido de punta a punta
contra el dataset de 27 empresas:

```
CONFIRMADO  KZH161209V32   EFOS_69B              $2,245,374.00
DESCARTADO  KZR170308INT   (sin evidencia referenciada)
CONFIRMADO  LSM120111199   KICKBACK_CIRCULAR     $1,275,488.33
CONFIRMADO  MRY1312186JL   INGRESO_NO_DECLARADO  $  847,961.06
CONFIRMADO  MXZ220310ALK   KICKBACK_CIRCULAR     $  627,727.24
CONFIRMADO  QXT121125K07   SIN_MATERIALIDAD      $  949,764.83
CONFIRMADO  VRH22081728I   KICKBACK_CIRCULAR     $1,315,556.03

6 confirmados · 1 descartado · $7,261,871.49 · 45-70s
0 cifras/identificadores inventados en el expediente final (verificado contra fraud.db)
```

Entre corridas, el número exacto de confirmados puede variar en ±1 (Qwen no es
100% determinista turno a turno pese a `temperature=0` en el loop de
herramientas); lo que no varía es que el Verificador nunca deja pasar un caso
sin evidencia real — cuando algo falla, falla como descarte correcto, no como
falsa confirmación.

## Limitaciones conocidas

- El Investigador sigue un checklist fijo de 5 pasos en vez de "formar una
  teoría y cambiar de rumbo" de forma verdaderamente adaptativa — fue una
  decisión deliberada para ganar confiabilidad, a costa de flexibilidad.
- Los 5 detectores de triage son heurísticas de texto/monto simples, no
  aprendizaje — por diseño, coinciden con lo que pide el brief ("simple
  detectors"), pero pueden dejar pasar esquemas de fraude que no se parezcan
  a ninguno de los patrones cableados.
- El frontend (`app.py`) no se ha probado con un usuario real subiendo un
  archivo por la interfaz web — se verificó que arranca sin errores y que
  consume el mismo generador que el CLI, pero falta la prueba manual completa
  de principio a fin en el navegador.
