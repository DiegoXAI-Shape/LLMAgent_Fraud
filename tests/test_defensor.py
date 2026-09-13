"""Pruebas de las guardas del Defensor.

Estas guardas existen porque el Defensor habla ante un auditor, y ahí una
afirmación que no se sostiene cuesta más que un silencio. Cada caso de abajo
corresponde a una falla que de verdad ocurrió — no son hipótesis.

Un detalle de diseño que las pruebas fijan: las guardas ADVIERTEN, no bloquean.
Por eso también se prueban los casos que NO deben disparar. Una alerta roja
sobre una respuesta correcta desacredita justo lo que sí se sostiene, así que un
falso positivo aquí es tan grave como un falso negativo.

    python -m unittest discover -s tests -t .
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config
from agents.defensor import (
    _contradice_expediente,
    _contradice_situacion_69b,
    _monto_no_corresponde_al_rfc,
)

HAY_BASE = config.DB_PATH.exists()


class NegarUnaImputacionConfirmada(unittest.TestCase):
    """El dictamen no se relitiga: el Defensor no puede desdecirse del expediente.

    Nació de una falla real: ante "¿por qué acusaste a esta empresa?", el modelo
    respondió "No acusé a GDH210804LCC..." sobre un caso que el expediente daba
    por confirmado. No inventó ningún dato — invirtió la conclusión.
    """

    CONFIRMADOS = [{"rfc_imputado": "KZH161209V32"}]

    def _detecta(self, texto: str) -> bool:
        return bool(_contradice_expediente(texto, self.CONFIRMADOS))

    def test_niega_la_imputacion_en_la_misma_oracion(self):
        for texto in (
            "No acuse a KZH161209V32 porque no hay pruebas.",
            "Se descarto a KZH161209V32 por falta de evidencia.",
            "No se imputo a KZH161209V32 en esta revision.",
        ):
            with self.subTest(texto=texto):
                self.assertTrue(self._detecta(texto))

    def test_afirmar_la_imputacion_no_dispara(self):
        for texto in (
            "Acuse a KZH161209V32 por deducir facturas simuladas.",
            "KZH161209V32 fue confirmado con prueba documental.",
        ):
            with self.subTest(texto=texto):
                self.assertFalse(self._detecta(texto))

    def test_negacion_en_otra_oracion_no_dispara(self):
        # El falso positivo que hubo que corregir: la negación pertenecía a la
        # oración anterior, del otro lado del signo de puntuación.
        for texto in (
            "En esta corrida no hubo leads descartados; el expediente solo imputo a KZH161209V32.",
            "No hay ciclos kickback. El expediente imputo a KZH161209V32.",
            "No se encontraron mas casos: KZH161209V32 quedo confirmado.",
        ):
            with self.subTest(texto=texto):
                self.assertFalse(self._detecta(texto))


@unittest.skipUnless(HAY_BASE, "requiere fraud.db con el catálogo 69-B cargado")
class DeclararMalLaSituacion69B(unittest.TestCase):
    """Confundir PRESUNTO con DEFINITIVO cambia si la imputación se sostiene.

    Sobre un PRESUNTO la acusación de EFOS_69B NO se sostiene: todavía puede
    desvirtuarse. El Defensor llegó a decir "solo presunta" de una empresa que su
    propia respuesta anterior había declarado DEFINITIVO.
    """

    @classmethod
    def setUpClass(cls):
        import sqlite3
        conn = sqlite3.connect(f"file:{config.DB_PATH}?mode=ro", uri=True)
        try:
            fila_d = conn.execute(
                "SELECT rfc FROM sat_blacklist_69b WHERE situacion='DEFINITIVO' LIMIT 1").fetchone()
            fila_p = conn.execute(
                "SELECT rfc FROM sat_blacklist_69b WHERE situacion='PRESUNTO' LIMIT 1").fetchone()
        finally:
            conn.close()
        cls.definitivo = fila_d[0] if fila_d else None
        cls.presunto = fila_p[0] if fila_p else None
        if cls.definitivo is None:
            raise unittest.SkipTest("el catálogo 69-B está vacío")

    def test_declarar_presunto_a_un_definitivo_dispara(self):
        texto = f"Aunque {self.definitivo} aparece en el 69-B, su situacion es solo presunta."
        self.assertTrue(_contradice_situacion_69b(texto))

    def test_declarar_bien_un_definitivo_no_dispara(self):
        texto = f"El proveedor {self.definitivo} esta en situacion DEFINITIVO del listado 69-B."
        self.assertFalse(_contradice_situacion_69b(texto))

    def test_declarar_definitivo_a_un_presunto_dispara(self):
        if self.presunto is None:
            self.skipTest("no hay ningún PRESUNTO en el catálogo")
        texto = f"El proveedor {self.presunto} tiene situacion DEFINITIVO."
        self.assertTrue(_contradice_situacion_69b(texto))

    def test_rfc_fuera_del_listado_no_dispara(self):
        self.assertFalse(_contradice_situacion_69b("La empresa AAA010101AAA no aparece en ningun listado."))


class MontoDeUnCasoAtribuidoAOtro(unittest.TestCase):
    """Dos casos reales, con sus números intercambiados por el modelo.

    Nació de una falla real, encontrada probando la interfaz en el navegador:
    ante "¿cuál es el caso con el monto más alto?", el Defensor le atribuyó a
    VRH22081728I (KICKBACK_CIRCULAR, $1,315,556.03) el monto y el esquema de
    KZH161209V32 (EFOS_69B, $2,245,374.00). Reproducido 5 de 5 veces contra el
    modelo local con la misma pregunta: comparar varias cifras en prosa es
    justo el tipo de tarea en la que un modelo chico falla. Ninguno de los dos
    números estaba inventado — la guarda de identificadores no lo atrapaba.
    """

    CONFIRMADOS = [
        {"rfc_imputado": "KZH161209V32", "monto_total_evidencia": 2245374.00,
         "evidencia": [{"monto": 1684780.56}, {"monto": 560593.44}]},
        {"rfc_imputado": "VRH22081728I", "monto_total_evidencia": 1315556.03,
         "evidencia": [{"monto": 647761.09}, {"monto": 667794.94}]},
    ]

    def _detecta(self, texto: str) -> list[str]:
        return _monto_no_corresponde_al_rfc(texto, self.CONFIRMADOS)

    def test_atrapa_la_falla_real_tal_como_ocurrio(self):
        # Reconstruido de la captura de pantalla real del interrogatorio.
        texto = (
            "The case with the highest amount is VRH22081728I, with 2,245,374.00 MXN "
            "per EFOS_69B scheme. 1,315,556.03 MXN por ciclo de kickback circular. "
            "El segundo caso más alto es KZH161209V32, con"
        )
        resultado = self._detecta(texto)
        self.assertTrue(resultado)
        # Se detecta en las dos direcciones: a cada RFC se le atribuyó el
        # monto del otro.
        self.assertTrue(any("KZH161209V32" in r for r in resultado))
        self.assertTrue(any("VRH22081728I" in r for r in resultado))

    def test_el_monto_correcto_junto_a_su_propio_rfc_no_dispara(self):
        texto = "VRH22081728I fue confirmado con $1,315,556.03 MXN por kickback circular."
        self.assertFalse(self._detecta(texto))

    def test_el_error_de_ranking_no_confunde_esta_guarda(self):
        # Un monto que SÍ pertenece a su propio RFC, aunque la afirmación de
        # que es "el más alto" sea falsa (LSM no es el máximo de la lista) —
        # ese es un error de comparación, no de emparejamiento, y lo corrige
        # la regla 8 del prompt (usar SQL), no esta guarda.
        texto = "El monto mas alto es el del RFC LSM120111199 con $1,275,488.33 MXN."
        confirmados_con_lsm = self.CONFIRMADOS + [
            {"rfc_imputado": "LSM120111199", "monto_total_evidencia": 1275488.33, "evidencia": []},
        ]
        self.assertFalse(_monto_no_corresponde_al_rfc(texto, confirmados_con_lsm))

    def test_sin_confirmados_no_revienta(self):
        self.assertEqual(_monto_no_corresponde_al_rfc("cualquier texto con $100.00", []), [])


if __name__ == "__main__":
    unittest.main()
