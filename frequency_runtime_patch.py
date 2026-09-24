"""Instalacao unica do parser real de frequencia individual do CGD.

Este modulo e a unica camada autorizada a substituir o parser legado de
frequencia. A instalacao e idempotente para funcionar tanto no processo
principal quanto nos workers do ProcessPoolExecutor no Windows.
"""

import re
import scraper
from frequencia_parser_cgd import parse_frequency_page

_DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_STATUS_MARKERS = (
    "presente",
    "faltou",
    "reposição",
    "reposicao",
    "compareceu",
    "ausente",
)

_PATCH_ATTR = "_cfis_frequency_patch_installed"


def _frequency_body(page):
    try:
        return " ".join(page.locator("body").inner_text().split()).lower()
    except Exception:
        return ""


def _wait_frequency_render(page):
    """Aguarda somente a renderizacao necessaria da rota individual."""
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


def install_frequency_parser():
    """Instala o parser oficial e os wrappers exatamente uma vez por processo."""
    if getattr(scraper, _PATCH_ATTR, False):
        return

    original_open_page = getattr(scraper, "_cfis_frequency_original_open_page", None)
    original_contract_bundle = getattr(scraper, "_cfis_frequency_original_contract_bundle", None)

    if original_open_page is None:
        original_open_page = scraper.open_page
        scraper._cfis_frequency_original_open_page = original_open_page

    if original_contract_bundle is None:
        original_contract_bundle = scraper.contract_bundle
        scraper._cfis_frequency_original_contract_bundle = original_contract_bundle

    def _open_page_frequency_aware(page, url, u, n, wait=None):
        url_lower = (url or "").lower()
        is_frequency = "/contratos/frequencias/" in url_lower
        is_contract = bool(re.search(r"/contratos/\d+/?(?:$|[?#])", url_lower))
        effective_wait = wait
        if is_frequency:
            effective_wait = max(1500, int(wait or 0))
        elif is_contract:
            effective_wait = max(1200, int(wait or 0))
        ok = original_open_page(page, url, u, n, effective_wait)
        if ok and is_frequency:
            _wait_frequency_render(page)
        return ok

    def _contract_bundle_with_real_frequency(page, cid, u, reps):
        aluno = original_contract_bundle(page, cid, u, reps)
        if not aluno:
            return aluno

        freq = getattr(page, "_cfis_last_frequency", None)
        if not isinstance(freq, dict):
            # O bundle legado pode ter sido executado sem passar pela rota de
            # frequencia; ainda assim marcamos explicitamente o estado para que
            # a validacao nunca trate isso como parser ausente.
            aluno["frequencia_status"] = "SEM_FREQUENCIA_A_INVESTIGAR"
            aluno.setdefault("frequencia_raw", [])
            aluno.setdefault("faltas", 0)
            aluno.setdefault("presencas", 0)
            aluno.setdefault("frequencia_reposicoes_cgd", 0)
            return aluno

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
    setattr(scraper, _PATCH_ATTR, True)


def validate_frequency_parser():
    """Falha cedo se outro modulo tiver tomado o lugar do parser oficial."""
    install_frequency_parser()
    parser = scraper.extract_frequency
    bundle = scraper.contract_bundle
    if parser is not _parse_frequency:
        raise RuntimeError(
            "PARSER_FREQUENCIA_INCORRETO: scraper.extract_frequency nao aponta "
            "para frequency_runtime_patch._parse_frequency"
        )
    if getattr(parser, "__module__", None) != __name__:
        raise RuntimeError(
            "PARSER_FREQUENCIA_INCORRETO: modulo do parser "
            f"{getattr(parser, '__module__', None)!r}"
        )
    if not getattr(bundle, "__name__", "").startswith("_contract_bundle_with_real_frequency"):
        raise RuntimeError(
            "BUNDLE_FREQUENCIA_INCORRETO: scraper.contract_bundle nao usa o "
            "wrapper de frequencia real"
        )
    print("PARSER_FREQUENCIA_VALIDADO=OK", flush=True)


install_frequency_parser()
print("PATCH_FREQUENCIA_REAL=OK", flush=True)
