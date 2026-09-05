"""Executor de detalhes em thread isolada, preservando a captura funcional do CGD."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing as mp
import os
import time
from urllib.parse import urlparse

import scraper
import scraper_runner


_original_contract_bundle = scraper.contract_bundle
_original_strict_open_page = scraper_runner._strict_open_page


_FALTA_TOKENS = {
    "faltou",
    "falta",
    "ausente",
    "nao compareceu",
    "não compareceu",
}
_PRESENTE_TOKENS = {
    "presente",
    "presenca",
    "presença",
    "compareceu",
}


def _recalcular_frequencia(aluno):
    """Corrige apenas os totais a partir de frequencia_raw já capturada."""
    registros = aluno.get("frequencia_raw") or []
    faltas = 0
    presencas = 0
    for registro in registros:
        candidatos = []
        status = registro.get("status")
        if status:
            candidatos.append(scraper.low(status))
        candidatos.extend(
            scraper.low(v) for v in (registro.get("valores") or []) if v is not None
        )
        if any(valor in _FALTA_TOKENS for valor in candidatos):
            faltas += 1
        elif any(valor in _PRESENTE_TOKENS for valor in candidatos):
            presencas += 1
    aluno["faltas"] = faltas
    aluno["presencas"] = presencas
    return aluno


def _curso_modal_preservado(page, cid, aluno):
    """Carrega a rota AJAX usada pelo CGD quando a tela principal não traz linhas."""
    if aluno.get("disciplinas"):
        return aluno
    url = f"{scraper.CGD_URL.rstrip('/')}/contratos/cursos/modal-demonstrativo-cursos/{cid}"
    try:
        _strict_open_page_com_espera_dinamica(page, url, aluno.get("unidade") or "cgd", f"cursos_modal_{cid}", 300)
        rows = scraper.extract_disciplines(page, url)
        if rows:
            rows, done, cur, fut = scraper.classify(rows)
            aluno["disciplinas"] = rows
            aluno["disciplinas_concluidas"] = done
            aluno["disciplinas_em_andamento"] = cur
            aluno["disciplinas_futuras"] = fut
            if cur:
                def num(r, k):
                    import re
                    m = re.search(r"\d+", str(r.get(k) or ""))
                    return int(m.group()) if m else -1
                aluno["progresso_atual"] = max(
                    cur,
                    key=lambda r: (num(r, "modulo"), num(r, "passo"), num(r, "progresso")),
                )
            print(f"[{aluno.get('unidade')}] CURSOS_MODAL_CAPTURADOS cid={cid} registros={len(rows)}")
        else:
            print(f"[{aluno.get('unidade')}] CURSOS_MODAL_SEM_REGISTROS cid={cid}")
    except Exception as exc:
        print(f"[{aluno.get('unidade')}] CURSOS_MODAL_ERRO cid={cid}: {exc!r}")
    return aluno


def _contract_bundle_preservado(page, cid, u, reps):
    aluno = _original_contract_bundle(page, cid, u, reps)
    if aluno:
        aluno = _recalcular_frequencia(aluno)
        aluno = _curso_modal_preservado(page, cid, aluno)
    return aluno


scraper.contract_bundle = _contract_bundle_preservado


def _tem_dados_dinamicos(page, etapa):
    try:
        tabelas = scraper.table_data(page)
        if any(rows for _, rows in tabelas):
            return True
    except Exception:
        pass
    texto = scraper.body(page)
    if not texto:
        return False
    low_text = scraper.low(texto)
    if "carregando..." in low_text:
        return False
    if etapa.startswith("frequencia"):
        return "resumo da frequencia" in low_text or "frequencia de cursos individuais" in low_text
    return True


def _strict_open_page_com_espera_dinamica(page, url, u, name, wait=None):
    page.goto(url, wait_until="domcontentloaded", timeout=scraper_runner.DETAIL_TIMEOUT_S * 1000)
    base_wait_ms = scraper.PAGE_WAIT_MS if wait is None else wait
    if base_wait_ms:
        page.wait_for_timeout(base_wait_ms)
    max_wait_s = max(5, int(os.getenv("CGD_AJAX_WAIT_S", "12")))
    deadline = time.monotonic() + max_wait_s
    ultimo_log = 0.0
    while time.monotonic() < deadline:
        if _tem_dados_dinamicos(page, name):
            break
        agora = time.monotonic()
        if agora - ultimo_log >= 2.0:
            print(f"[{u}] AGUARDANDO_AJAX etapa={name} url={page.url}")
            ultimo_log = agora
        page.wait_for_timeout(500)
    path = urlparse(page.url).path.rstrip("/").lower()
    if path == "/login" or path.startswith("/login/"):
        raise scraper_runner.SessionExpired(f"[{u}] SESSAO_EXPIRADA_NO_DETALHE etapa={name} url={url} final={page.url}")
    if urlparse(page.url).netloc != urlparse(scraper.CGD_URL).netloc:
        raise RuntimeError(f"[{u}] DETALHE_SAIU_DO_HOST etapa={name}: {page.url}")
    if name.startswith("frequencia"):
        tabelas = scraper.table_data(page)
        registros = sum(len(rows) for _, rows in tabelas)
        print(f"[{u}] FREQUENCIA_DOM_PRONTA registros_tabela={registros} url={page.url}")
    print(f"[{u}] {name}: {page.url}")
    try:
        scraper.dump(page, u, name)
    except Exception:
        pass
    return True


def _run_details_in_thread(u, cfg, contracts, reps, storage_state):
    previous_strict = scraper_runner._strict_open_page
    scraper_runner._strict_open_page = _strict_open_page_com_espera_dinamica
    try:
        return scraper_runner.safe_process_details(u, cfg, contracts, reps, storage_state)
    finally:
        scraper_runner._strict_open_page = previous_strict


def threaded_process_details(u, cfg, contracts, reps, storage_state):
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cgd-detail") as pool:
        return pool.submit(_run_details_in_thread, u, cfg, contracts, reps, storage_state).result()


scraper.discover_contracts = scraper_runner.optimized_discover_contracts
scraper.process_details = threaded_process_details


if __name__ == "__main__":
    mp.freeze_support()
    scraper.main()
