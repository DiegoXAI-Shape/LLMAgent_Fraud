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

---

## Estado actual (verificado, no aspiracional)

Corrida completa del pipeline (triage → investigación → verificación),
2 veces seguidas, mismos resultados ambas:

```
OK  DVR200419828   KICKBACK_CIRCULAR   $1,467,192.40
OK  FLS121103F19   KICKBACK_CIRCULAR   $1,489,880.94
OK  HCZ181008TJY   SIN_MATERIALIDAD    $  819,709.95
NO  KZR170308INT   (descartado)         — falso positivo del detector 4, limpiado solo
OK  LCR12092415J   EFOS_69B            $1,697,425.10
OK  XCG1911198RK   KICKBACK_CIRCULAR   $1,444,503.86

~53s para 6 investigaciones · 0 falsas confirmaciones · 0 truncamientos
```

## Limitaciones conocidas

- El Investigador sigue un checklist fijo de 5 pasos en vez de "formar una
  teoría y cambiar de rumbo" de forma verdaderamente adaptativa — fue una
  decisión deliberada para ganar confiabilidad, a costa de flexibilidad.
- Los 4 detectores de triage son heurísticas de texto/monto simples, no
  aprendizaje — por diseño, coinciden con lo que pide el brief ("simple
  detectors"), pero pueden dejar pasar esquemas de fraude que no se parezcan
  a ninguno de los 4 patrones cableados.
- `auditor.py` (Gemini) no se ha probado end-to-end en esta bitácora — la
  bitácora cubre Fases 0-2 (ingesta, investigación, verificación).
