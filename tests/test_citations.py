import unittest

from citations import extraer_citas


class ExtraerCitasTests(unittest.TestCase):
    def test_reconoce_formatos_uruguayos_y_conserva_el_orden(self):
        texto = (
            "La Ley N.º 19.355 y el Decreto N.º 152/013 se aplican según "
            "la Sentencia N.º 123/2020 y el IUE 123-45/2020."
        )

        citas = extraer_citas(texto)

        self.assertEqual(
            [(c['tipo'], c['numero'], c['anio']) for c in citas],
            [
                ('ley', '19355', None),
                ('decreto', '152', '2013'),
                ('jurisprudencia', '123', '2020'),
                ('jurisprudencia', '123-45', '2020'),
            ],
        )

    def test_no_duplica_la_misma_cita(self):
        citas = extraer_citas("Ley 19.355; ley N° 19.355; Ley 19.355")
        self.assertEqual(len(citas), 1)
        self.assertEqual(citas[0]['numero'], '19355')

    def test_no_clasifica_decreto_ley_como_ley(self):
        citas = extraer_citas("Decreto-Ley 14.500 y Ley 18.331")
        self.assertEqual([c['tipo'] for c in citas], ['decreto_ley', 'ley'])

    def test_reconoce_numeros_normativos_contextualizados(self):
        citas = extraer_citas(
            "La Ley: N° 18.331 se complementa con el Decreto: N° 152/013. "
            "También se cita el Decreto N° 152."
        )
        self.assertEqual(
            [(c['tipo'], c['numero'], c['anio']) for c in citas],
            [('ley', '18331', None), ('decreto', '152', '2013'), ('decreto', '152', None)],
        )

    def test_reconoce_prefijos_jurisprudenciales(self):
        citas = extraer_citas("SCJ N° 123/2020, TAT 45/2021 y DFA 12/2019")
        self.assertEqual(
            [(c['tipo'], c['prefijo'], c['numero'], c['anio']) for c in citas],
            [
                ('jurisprudencia', 'SCJ', '123', '2020'),
                ('jurisprudencia', 'TAT', '45', '2021'),
                ('jurisprudencia', 'DFA', '12', '2019'),
            ],
        )


if __name__ == '__main__':
    unittest.main()
