"""Pruebas de las piezas deterministas del núcleo.

Se usa `unittest` (biblioteca estándar) y NO pytest a propósito: correr las
pruebas no debe exigir instalar nada. La máquina donde se haga la demostración
puede no tener pytest, y una dependencia más es una cosa más que puede fallar
ese día — el mismo criterio que llevó a elegir `fpdf2` sobre `weasyprint` y a
dibujar el rastro del dinero con DOT en el navegador en vez del binario de
Graphviz.

    python -m unittest discover -s tests -t .

La mayoría de estas pruebas no tocan la base de datos: verifican funciones puras
que son justamente las que sostienen las garantías del sistema (el dígito
verificador de un RFC, el diagrama del flujo, el expediente en PDF). Las que sí
la necesitan se saltan solas si `fraud.db` no existe, para que la suite corra en
un repo recién clonado.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pandas as pd

import config
from core.money_trail import construir_dot
from core.report_pdf import construir_pdf
from core.rfc import digito_verificador, rfc_valido
from data_pipeline.ingest import _parse_bool
from data_pipeline.universal_loader import cargar_tabular

HAY_BASE = config.DB_PATH.exists()


class DigitoVerificadorRFC(unittest.TestCase):
    """El último carácter de un RFC se calcula de los otros once.

    Los valores vienen del caso real de la bitácora 15: un CFDI de nómina cuyo
    RFC emisor la visión leyó mal de forma reproducible.
    """

    def test_rfc_real_de_persona_moral_pasa(self):
        self.assertTrue(rfc_valido("ADR531130N5A"))

    def test_rfc_de_persona_fisica_pasa(self):
        # RFC sintético de 13 caracteres derivado del genérico del SAT. NO se usa
        # aquí el RFC real que aparecía en el CFDI de prueba: un RFC de persona
        # física codifica iniciales y fecha de nacimiento, y este repositorio es
        # público. Para probar el algoritmo da exactamente igual.
        self.assertTrue(rfc_valido("XAXX010101004"))

    def test_el_rfc_que_la_vision_leyo_mal_falla(self):
        # Qwen devolvió esto 4 de 4 veces sobre la misma imagen.
        self.assertFalse(rfc_valido("ADSR51130N5A"))

    def test_dice_cual_era_el_digito_esperado(self):
        self.assertEqual(digito_verificador("ADSR51130N5A"), "8")

    def test_basura_no_revienta_y_no_se_declara_valida(self):
        for entrada in ("", "XXX", "no-es-un-rfc-12345", "ADR531130N5"):
            with self.subTest(entrada=entrada):
                # None = "no evaluable", que NO es lo mismo que "inválido".
                self.assertIsNone(rfc_valido(entrada))


class RastroDelDinero(unittest.TestCase):
    """`construir_dot` dibuja solo lo que la evidencia verificada respalda."""

    LEAD = {
        "rfc_imputado": "GDH210804LCC",
        "razon_social": "Galindo-Sanabria S.C.",
        "tipo_esquema": "EFOS_69B",
        "evidencia": [{
            "tipo": "factura", "uuid": "0a92940f-03bd-4202-a6cc-54bf9ce20747",
            "monto": 184354.60, "existe": True,
            "emisor_rfc": "BJB220420XVT", "receptor_rfc": "GDH210804LCC",
        }],
    }

    def test_dibuja_las_dos_contrapartes_y_el_monto(self):
        dot = construir_dot(self.LEAD)
        self.assertIsNotNone(dot)
        self.assertIn("digraph", dot)
        self.assertIn("BJB220420XVT", dot)
        self.assertIn("GDH210804LCC", dot)
        self.assertIn("184,354.60", dot)

    def test_sin_evidencia_no_dibuja_nada(self):
        # Un diagrama vacío sugeriría que no hubo hallazgo: mejor no mostrar nada.
        self.assertIsNone(construir_dot({"rfc_imputado": "AAA010101AAA", "evidencia": []}))

    def test_lead_vacio_no_revienta(self):
        self.assertIsNone(construir_dot({}))

    def test_evidencia_inexistente_se_ignora(self):
        lead = dict(self.LEAD, evidencia=[dict(self.LEAD["evidencia"][0], existe=False)])
        self.assertIsNone(construir_dot(lead))


class ExcelDeUnTercero(unittest.TestCase):
    """Una hoja ajena a la que le falta una columna OPCIONAL debe traducirse igual.

    Falla real, medida con un Excel de un tercero con nombres de hoja y columna
    completamente distintos: 'Sujeta a Revision' no tenía sinónimo y el modelo
    no la mapeó. Como `cargar_tabular` solo conservaba las columnas que sí
    logró mapear, la hoja 'Entidades' resultante no tenía la columna
    `es_empresa_auditada` — que ni siquiera es obligatoria — e `ingest.py`
    truena con "faltan columnas" sobre TODA la hoja. El fraude sembrado nunca
    llegó a investigarse: el sistema se negaba a trabajar por una columna que
    no necesitaba, en el paso 0, antes de que el detective hiciera nada.
    """

    def test_columna_opcional_faltante_se_rellena_en_vez_de_tumbar_la_hoja(self):
        crudas = pd.DataFrame([{
            "Clave RFC": "AAA010101AAA",
            "Denominacion o Razon Social": "Empresa de Prueba SA de CV",
            "C.P. del Domicilio": "64000",
            # Sin equivalente de es_empresa_auditada -- a propósito.
        }])
        with tempfile.TemporaryDirectory() as carpeta:
            ruta = Path(carpeta) / "Catalogo de Clientes.xlsx"
            crudas.to_excel(ruta, sheet_name="Catalogo de Clientes", index=False)
            hojas, reporte = cargar_tabular(ruta, usar_modelo=False)

        self.assertIn("Entidades", hojas)
        # La columna existe (para que ingest.py no truene), pero vacía: no se
        # inventa un valor para un dato que el archivo nunca trajo.
        self.assertIn("es_empresa_auditada", hojas["Entidades"].columns)
        self.assertTrue(pd.isna(hojas["Entidades"].loc[0, "es_empresa_auditada"]))
        self.assertTrue(any("es_empresa_auditada" in linea for linea in reporte))

    def test_una_celda_vacia_no_se_lee_como_verdadera(self):
        # bool(float('nan')) es True en Python -- por eso este chequeo es
        # explícito y no un "obviamente funciona". Sin él, rellenar la columna
        # faltante marcaría a TODAS las empresas del Excel ajeno como
        # auditadas, cambiando a quién investiga el sistema.
        self.assertEqual(_parse_bool(float("nan")), 0)
        self.assertEqual(_parse_bool(pd.NA), 0)


class ExpedienteEnPDF(unittest.TestCase):
    """El PDF se arma desde los leads verificados, no desde el texto del modelo."""

    CONFIRMADOS = [{
        "rfc_imputado": "KZH161209V32", "razon_social": "Industrias Jaimes y Centeno",
        "tipo_esquema": "EFOS_69B", "monto_total_evidencia": 2245374.00,
        "regla_fiscal": "Artículo 69-B del CFF", "narrativa": "Dedujo comprobantes simulados.",
        "evidencia": [{"tipo": "factura", "uuid": "3ec97b40-582f-48df-90ac-7ca52d4e3ec1",
                       "monto": 1684780.56, "contado_en_total": True}],
    }]
    DESCARTADOS = [{"lead": {"rfc_imputado": "KZR170308INT", "tipo_esquema": None},
                    "razon": "DESCARTADO_FALTA_EVIDENCIA: no hay evidencia referenciada."}]

    def test_genera_un_pdf_con_contenido(self):
        with tempfile.TemporaryDirectory() as carpeta:
            destino = Path(carpeta) / "expediente.pdf"
            ruta = construir_pdf(self.CONFIRMADOS, self.DESCARTADOS, destino,
                                 archivo_origen="prueba.xlsx", redactado_por="plantilla")
            self.assertTrue(ruta.exists())
            # Un PDF con portada, tabla resumen, un caso y descartados pesa
            # decenas de KB; menos de 5 KB significaría que salió vacío.
            self.assertGreater(ruta.stat().st_size, 5_000)

    def test_sin_casos_tampoco_revienta(self):
        with tempfile.TemporaryDirectory() as carpeta:
            destino = Path(carpeta) / "vacio.pdf"
            self.assertTrue(construir_pdf([], [], destino).exists())


class CuandoNoValeLaPenaReintentarAGemini(unittest.TestCase):
    """Distinguir una saturación pasajera de una cuota que no se repone hoy.

    Un 503 o un límite por minuto se resuelven esperando unos segundos: para eso
    existen los reintentos. Una cuota DIARIA agotada no — se repone al día
    siguiente, y cada reintento solo agrega espera muerta antes de caer a la
    plantilla determinista. Se midió: tres intentos contra la cuota diaria
    agregaron ~22 segundos a una corrida de poco más de un minuto.

    Los textos de abajo son los que devolvió la API de verdad, no inventados.
    """

    CUOTA_DIARIA = (
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded your "
        "current quota', 'status': 'RESOURCE_EXHAUSTED', 'details': [{'quotaId': "
        "'GenerateRequestsPerDayPerProjectPerModel-FreeTier'}]}}"
    )
    SATURACION = (
        "503 UNAVAILABLE. {'error': {'code': 503, 'message': 'This model is currently "
        "experiencing high demand. Spikes in demand are usually temporary.'}}"
    )
    LIMITE_POR_MINUTO = (
        "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'details': [{'quotaId': "
        "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier'}]}}"
    )

    def _corta(self, texto: str) -> bool:
        from agents.auditor import _es_cuota_diaria_agotada
        return _es_cuota_diaria_agotada(Exception(texto))

    def test_la_cuota_diaria_corta_los_reintentos(self):
        self.assertTrue(self._corta(self.CUOTA_DIARIA))

    def test_lo_pasajero_sigue_reintentandose(self):
        # Aquí un falso positivo costaría caro: dejaría de reintentar un error
        # que sí se resuelve solo, y el expediente perdería la prosa de Gemini
        # sin necesidad.
        for texto in (self.SATURACION, self.LIMITE_POR_MINUTO,
                      "connection reset by peer",
                      "No hay GEMINI_API_KEY configurada"):
            with self.subTest(texto=texto[:40]):
                self.assertFalse(self._corta(texto))


@unittest.skipUnless(HAY_BASE, "requiere fraud.db (se crea con la primera ingesta)")
class UmbralesYCiclos(unittest.TestCase):
    """Propiedades que deben cumplirse con cualquier dataset cargado."""

    def test_los_umbrales_son_positivos_y_respetan_su_piso(self):
        from core import tools
        self.assertGreaterEqual(tools.umbral_ciclo_monto(), config.PISO_CICLO_MONTO)
        self.assertGreaterEqual(tools.umbral_ingreso_sin_factura(), config.PISO_INGRESO_SIN_FACTURA)

    def test_ningun_ciclo_reportado_tiene_menos_de_tres_nodos(self):
        # Un "ciclo" de dos nodos es comercio bilateral normal ("A me paga y yo
        # le pago"), no round-tripping. Aceptarlos produjo falsas acusaciones de
        # lavado sobre empresas que solo se venden entre sí (bitácora 24).
        from core import tools
        for ciclo in tools.find_money_cycles(min_amount=1.0, max_hops=8):
            with self.subTest(ciclo=ciclo["ciclo_rfcs"]):
                self.assertGreaterEqual(len(ciclo["ciclo_rfcs"]), config.MIN_NODOS_CICLO)

    def test_solo_se_permiten_consultas_de_lectura(self):
        from core.tools import QueryNotAllowedError, query_database
        for sql in ("DELETE FROM invoices",
                    "SELECT 1; DROP TABLE invoices",
                    "UPDATE entities SET razon_social = 'x'"):
            with self.subTest(sql=sql):
                with self.assertRaises(QueryNotAllowedError):
                    query_database(sql)


if __name__ == "__main__":
    unittest.main()
