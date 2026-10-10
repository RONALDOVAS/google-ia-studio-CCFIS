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
