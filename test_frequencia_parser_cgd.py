import unittest

from frequencia_parser_cgd import parse_frequency_text


class FrequenciaParserCGDTests(unittest.TestCase):
    def test_presenca_falta_e_reposicao(self):
        text = """
        Frequência de Cursos Individuais
        Data  Horário Sala Curso Aula Conteúdo Obs Lição Passo Seq
        01/09/2026 08:00 A Curso 1 Aula 1 Conteúdo Presente 1 1 1
        02/09/2026 08:00 A Curso 1 Aula 2 Conteúdo Faltou 2 1 2
        03/09/2026 08:00 A Curso 1 Aula 3 Conteúdo Reposição 3 1 3
        04/09/2026 08:00 A Curso 1 Aula 4 Conteúdo Reposição-Faltou 4 1 4
        Frequência de turmas
        """
        parsed = parse_frequency_text(text)

        self.assertEqual(parsed["presencas"], 1)
        self.assertEqual(parsed["faltas"], 1)
        self.assertEqual(parsed["reposicoes"], 2)
        self.assertEqual(len(parsed["registros"]), 4)
        self.assertEqual(parsed["registros"][0]["classificacao"], "presenca")
        self.assertEqual(parsed["registros"][1]["classificacao"], "falta")
        self.assertEqual(parsed["registros"][2]["classificacao"], "reposicao")
        self.assertEqual(parsed["registros"][3]["classificacao"], "reposicao_faltou")

    def test_reposicao_nao_vira_falta(self):
        text = """
        Frequência de Cursos Individuais
        10/08/2026 08:00 Curso Conteúdo Reposição 1 1 1
        Frequência de turmas
        """
        parsed = parse_frequency_text(text)

        self.assertEqual(parsed["presencas"], 0)
        self.assertEqual(parsed["faltas"], 0)
        self.assertEqual(parsed["reposicoes"], 1)

    def test_frequencia_zero_nao_cria_registro_falso(self):
        text = """
        Frequência de Cursos Individuais
        Nenhum registro de frequência disponível.
        Frequência de turmas
        """
        parsed = parse_frequency_text(text)

        self.assertEqual(parsed["presencas"], 0)
        self.assertEqual(parsed["faltas"], 0)
        self.assertEqual(parsed["reposicoes"], 0)
        self.assertEqual(parsed["registros"], [])


if __name__ == "__main__":
    unittest.main()
