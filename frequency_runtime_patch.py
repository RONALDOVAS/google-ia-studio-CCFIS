"""Patch operacional da frequencia individual real do CGD.

A frequencia individual e carregada por JavaScript. A rota pode responder
antes de o texto dos registros estar disponivel no DOM; por isso este patch
faz uma espera curta e direcionada antes de executar o parser.

A fonte continua sendo a rota individual do contrato/aluno. A tela coletiva
Registrar Frequencia nao substitui este historico individual.
"""
import re
import scraper
from frequencia_parser_cgd import parse_frequency_page

_original_open_page = scraper.open_page
_original_contract_bundle = scraper.contract_bundle

_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_STATUS_MARKERS = (
    "presente",
    "faltou",
    "reposição",
    "reposicao",
    "compareceu",
    "ausente",
)


def _frequency_body(page):
    try:
        return " ".join(page.locator("body").inner_text().split()).lower()
    except Exception:
        return ""


def _wait_frequency_render(page):
    """Aguarda o conteúdo individual renderizado sem depender de networkidle."""
    for _ in range(10):
        text = _frequency_body(page)
        has_date = bool(_DATE_RE.search(text))
        has_status = any(marker in text for marker in _STATUS_MARKERS)
        has_frequency_context = (
            "frequência de cursos individuais" in text
            or "frequencia de cursos individuais" in text
        )
        if (has_frequency_context and has_date) or (has_date and has_status):
            return
        page.wait_for_timeout(500)


def _open_page_frequency_aware(page, url, u, n, wait=None):
    if "/contratos/frequencias/" in (url or "").lower():
        wait = max(1500, int(wait or 0))
    ok = _original_open_page(page, url, u, n, wait)
    if ok and "/contratos/frequencias/" in (url or "").lower():
        _wait_frequency_render(page)
    return ok


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
