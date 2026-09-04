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
    """Corrige apenas os totais a partir de frequencia_raw já capturada.

    O parser original do scraper é preservado para não alterar o caminho que
    comprovadamente capturou os detalhes reais no Run #171. No CGD, o estado
    costuma aparecer na coluna 'Aula', portanto também pode estar dentro de
    'valores' quando 'status' vem vazio.
    """
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

        achou_falta = any(valor in _FALTA_TOKENS for valor in candidatos)
        achou_presenca = any(valor in _PRESENTE_TOKENS for valor in candidatos)

        if achou_falta:
            faltas += 1
        elif achou_presenca:
            presencas += 1

    aluno["faltas"] = faltas
    aluno["presencas"] = presencas
    return aluno


def _contract_bundle_preservado(page, cid, u, reps):
    """Usa integralmente a captura comprovada e altera somente os totais."""
    aluno = _original_contract_bundle(page, cid, u, reps)
    if aluno:
        aluno = _recalcular_frequencia(aluno)
    return aluno


scraper.contract_bundle = _contract_bundle_preservado


def _tem_dados_dinamicos(page, etapa):
    """Detecta se o conteudo AJAX relevante da etapa ja apareceu no DOM."""
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
    """Abre a pagina e espera a renderizacao AJAX antes da extracao."""
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
        raise scraper_runner.SessionExpired(
            f"[{u}] SESSAO_EXPIRADA_NO_DETALHE etapa={name} url={url} final={page.url}"
        )
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
    # scraper.main() mantém um contexto Playwright Sync aberto no thread principal.
    # O runner de detalhes usa outro contexto Sync e, por isso, roda em uma
    # thread sem loop asyncio para evitar o erro "Sync API inside asyncio loop".
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cgd-detail") as pool:
        return pool.submit(
            _run_details_in_thread,
            u,
            cfg,
            contracts,
            reps,
            storage_state,
        ).result()


scraper.discover_contracts = scraper_runner.optimized_discover_contracts
scraper.process_details = threaded_process_details


if __name__ == "__main__":
    mp.freeze_support()
    scraper.main()
