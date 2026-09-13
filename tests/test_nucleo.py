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

import config
from core.money_trail import construir_dot
from core.report_pdf import construir_pdf
from core.rfc import digito_verificador, rfc_valido

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
