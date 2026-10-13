import unittest

import cgd_http_detail
import scraper


class CGDDomainSemanticsTests(unittest.TestCase):
    def test_enrollment_date_is_not_academic_start_date(self):
        payloads = [{
            "url": "/contratos/12345",
            "data": {"contrato_id": "12345", "data_matricula": "2026-02-01"},
        }]
        fields = scraper._extract_json_domain_fields(payloads, "12345")
        self.assertEqual(fields.get("data_matricula"), "2026-02-01")
        self.assertNotIn("data_inicio", fields)

    def test_enrollment_control_is_not_mapped_to_data_inicio(self):
        html = """
        <table><tr><th>Data de matrícula</th><td>01/02/2026</td></tr></table>
        """
        fields = cgd_http_detail._structured_fields(html)
        self.assertEqual(fields.get("data_matricula"), "01/02/2026")
        self.assertNotIn("data_inicio", fields)

    def test_explicit_academic_start_label_is_extracted(self):
        html = """
        <table><tr><th>Data de início das aulas</th><td>10/02/2026</td></tr></table>
        """
        fields = cgd_http_detail._structured_fields(html)
        self.assertEqual(fields.get("data_inicio"), "10/02/2026")

    def test_domain_fields_parse_original_structured_html(self):
        html = """
        <table>
          <tr><th>Curso contratado</th><td>Windows</td></tr>
          <tr><th>Turma atual</th><td>Turma Noite A</td></tr>
          <tr><th>Professor responsável</th><td>Maria Silva</td></tr>
          <tr><th>Data de início das aulas</th><td>10/02/2026</td></tr>
        </table>
        """
        fields = cgd_http_detail._domain_fields(html, "", "", "", cid=None)
        self.assertEqual(fields.get("curso"), "Windows")
        self.assertEqual(fields.get("turma"), "Turma Noite A")
        self.assertEqual(fields.get("professor"), "Maria Silva")
        self.assertEqual(fields.get("data_inicio"), "2026-02-10")

    def test_flattened_labels_do_not_swallow_following_fields(self):
        text = "Curso: Windows Turma: Noite A Professor: Maria Silva Data de início: 10/02/2026"
        self.assertEqual(cgd_http_detail._label_value(text, ("Curso",)), "Windows")
        self.assertEqual(cgd_http_detail._label_value(text, ("Turma",)), "Noite A")
        self.assertEqual(cgd_http_detail._label_value(text, ("Professor",)), "Maria Silva")

    def test_ingestor_accepts_missing_turma_and_professor(self):
        from ingest_dados_alunos_supabase import normalize
        aluno, disciplines, errors = normalize({
            "contrato": "TESTE-123", "cgd_matricula_id": "TESTE-123",
            "nome": "Aluno Teste", "unidade": "matriz", "curso": "Windows",
            "data_inicio": "2026-02-10", "turma": None, "professor": None,
            "meses_contrato_total": 12, "disciplinas": [],
        })
        self.assertEqual(errors, [])
        self.assertIsNotNone(aluno)
        # Schema remoto indisponível: padrão NOT NULL-safe é string vazia.
        self.assertEqual(aluno["turma_nome"], "")
        self.assertEqual(aluno["professor_nome"], "")
        self.assertEqual(disciplines, [])

    def test_ingestor_parses_cgd_hour_values_with_units(self):
        from ingest_dados_alunos_supabase import num, nullable_num
        self.assertEqual(num("32h"), 32)
        self.assertEqual(num("24 horas"), 24)
        self.assertEqual(nullable_num("12,5 horas"), 12.5)

    def test_status_alone_does_not_infer_missing_assignment(self):
        domain = {"turma": None, "professor": None, "status_matricula": "trancado"}
        result = scraper._apply_assignment_fallback(domain, "matrícula trancada")
        self.assertIsNone(result["turma"])
        self.assertIsNone(result["professor"])

    def test_sem_turma_does_not_infer_missing_professor(self):
        domain = {"turma": None, "professor": None}
        result = scraper._apply_assignment_fallback(domain, "situação: sem turma")
        self.assertEqual(result["turma"], "SEM TURMA")
        self.assertIsNone(result["professor"])

    def test_sem_professor_does_not_infer_missing_turma(self):
        domain = {"turma": None, "professor": None}
        result = scraper._apply_assignment_fallback(domain, "situação: sem professor")
        self.assertIsNone(result["turma"])
        self.assertEqual(result["professor"], "NÃO ALOCADO")


if __name__ == "__main__":
    unittest.main()
