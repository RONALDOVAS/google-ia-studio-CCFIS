"""Patch operacional da frequencia real do CGD.

O CGD atual renderiza a frequencia individual via JavaScript e a tabela pode
nao existir como <table> no DOM. O parser dedicado trabalha sobre o texto
renderizado e e a fonte autoritativa para presencas, faltas e reposicoes.

Mantemos a arquitetura incremental e o lote/performance atuais. O patch
apenas troca o parser da frequencia e garante uma espera maior somente nessa
rota, porque ela precisa do render client-side antes da leitura.
"""
import scraper
from frequencia_parser_cgd import parse_frequency_page

_original_open_page = scraper.open_page
_original_contract_bundle = scraper.contract_bundle


def _open_page_frequency_aware(page, url, u, n, wait=None):
    if wait is None and "/contratos/frequencias/" in (url or "").lower():
        wait = 800
    return _original_open_page(page, url, u, n, wait)


def _parse_frequency(page, cid):
    result = parse_frequency_page(page)
    normalized = {
        "faltas": int(result.get("faltas") or 0),
        "presencas": int(result.get("presencas") or 0),
        "reposicoes": int(result.get("reposicoes") or 0),
        "registros": result.get("registros") or [],
    }
    page._cfis_last_frequency = normalized
    print(
        f"[FREQUENCIA_REAL] cid={cid} registros={len(normalized['registros'])} "
        f"presencas={normalized['presencas']} faltas={normalized['faltas']} "
        f"reposicoes={normalized['reposicoes']}",
        flush=True,
    )
    return normalized


def _contract_bundle_with_real_frequency(page, cid, u, reps):
    aluno = _original_contract_bundle(page, cid, u, reps)
    freq = getattr(page, "_cfis_last_frequency", None)
    if aluno and isinstance(freq, dict):
        registros = freq.get("registros") or []
        aluno["faltas"] = freq.get("faltas", aluno.get("faltas", 0))
        aluno["presencas"] = freq.get("presencas", aluno.get("presencas", 0))
        aluno["frequencia_raw"] = registros
        aluno["frequencia_reposicoes_cgd"] = freq.get("reposicoes", 0)
        datas = [
            str(r.get("data_iso") or "").strip()
            for r in registros
            if isinstance(r, dict) and str(r.get("data_iso") or "").strip()
        ]
        aluno["ultimo_acesso"] = max(datas) if datas else None
        aluno["frequencia_status"] = (
            "COM_FREQUENCIA_REAL" if registros else "SEM_FREQUENCIA_A_INVESTIGAR"
        )
    return aluno


scraper.open_page = _open_page_frequency_aware
scraper.extract_frequency = _parse_frequency
scraper.contract_bundle = _contract_bundle_with_real_frequency
print("PATCH_FREQUENCIA_REAL=OK", flush=True)
