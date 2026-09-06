"""Executor de detalhes em thread isolada, preservando a captura funcional do CGD."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing as mp
import os
import time

import scraper
import scraper_runner


_original_contract_bundle = scraper.contract_bundle

_FALTA_TOKENS = {"faltou", "falta", "ausente", "nao compareceu", "não compareceu"}
_PRESENTE_TOKENS = {"presente", "presenca", "presença", "compareceu"}


def _recalcular_frequencia(aluno):
    registros = aluno.get("frequencia_raw") or []
    faltas = 0
    presencas = 0
    for registro in registros:
        candidatos = []
        status = registro.get("status")
        if status:
            candidatos.append(scraper.low(status))
        candidatos.extend(scraper.low(v) for v in (registro.get("valores") or []) if v is not None)
        if any(valor in _FALTA_TOKENS for valor in candidatos):
            faltas += 1
        elif any(valor in _PRESENTE_TOKENS for valor in candidatos):
            presencas += 1
    aluno["faltas"] = faltas
    aluno["presencas"] = presencas
    return aluno


def _curso_modal_preservado(page, cid, aluno):
    if aluno.get("disciplinas"):
        return aluno
    url = f"{scraper.CGD_URL.rstrip('/')}/contratos/cursos/modal-demonstrativo-cursos/{cid}"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=scraper_runner.DETAIL_TIMEOUT_S * 1000)
        page.wait_for_timeout(scraper.PAGE_WAIT_MS)
        deadline = time.monotonic() + max(5, int(os.getenv("CGD_AJAX_WAIT_S", "12")))
        while time.monotonic() < deadline:
            rows = scraper.table_data(page)
            if any(rows for _, rows in rows):
                break
            page.wait_for_timeout(500)
        rows = scraper.extract_disciplines(page, url)
        if rows:
            rows, done, cur, fut = scraper.classify(rows)
            aluno["disciplinas"] = rows
            aluno["disciplinas_concluidas"] = done
            aluno["disciplinas_em_andamento"] = cur
            aluno["disciplinas_futuras"] = fut
            print(f"[{aluno.get('unidade')}] CURSOS_MODAL_CAPTURADOS cid={cid} registros={len(rows)}", flush=True)
        else:
            print(f"[{aluno.get('unidade')}] CURSOS_MODAL_SEM_REGISTROS cid={cid}", flush=True)
    except Exception as exc:
        print(f"[{aluno.get('unidade')}] CURSOS_MODAL_ERRO cid={cid}: {exc!r}", flush=True)
    return aluno


def _contract_bundle_preservado(page, cid, u, reps):
    aluno = _original_contract_bundle(page, cid, u, reps)
    if aluno:
        aluno = _recalcular_frequencia(aluno)
        aluno = _curso_modal_preservado(page, cid, aluno)
    return aluno


scraper.contract_bundle = _contract_bundle_preservado


def _run_details_in_thread(u, cfg, contracts, reps, storage_state):
    # O Playwright Sync API fica inteiramente dentro desta thread dedicada.
    # Não há monkey-patch de símbolos privados de scraper_runner.
    return scraper_runner.safe_process_details(u, cfg, contracts, reps, storage_state)


def threaded_process_details(u, cfg, contracts, reps, storage_state):
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cgd-detail") as pool:
        return pool.submit(_run_details_in_thread, u, cfg, contracts, reps, storage_state).result()


scraper.discover_contracts = scraper_runner.optimized_discover_contracts
scraper.process_details = threaded_process_details


if __name__ == "__main__":
    mp.freeze_support()
    scraper.main()
