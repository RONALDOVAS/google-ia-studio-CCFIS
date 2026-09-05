"""Executor CGD com detalhe HTTP paralelo e fallback Edge.

A autenticacao e a descoberta das paginas continuam no executor existente.
O detalhe de cada contrato e feito por requests autenticado, reutilizando os
cookies salvos pelo Edge. O Edge fica reservado para excecoes reais.
"""

from concurrent.futures import ThreadPoolExecutor, as_completed
import multiprocessing as mp
import os

import scraper
import scraper_runner

DETAIL_HTTP_WORKERS = max(1, int(os.getenv("CGD_DETAIL_HTTP_WORKERS", os.getenv("CGD_DETAIL_WORKERS", "4"))))
DETAIL_LIMIT = max(0, int(os.getenv("CGD_DETAIL_LIMIT", "0")))
DETAIL_RETRIES = max(0, int(os.getenv("CGD_DETAIL_RETRIES", "1")))


def _http_one(cid, u, reps, storage_state):
    session = scraper_runner._session_from_storage_state(storage_state)
    return scraper_runner._http_contract_bundle(session, cid, u, reps)


def _http_process(u, cfg, contracts, reps, storage_state):
    targets = list(contracts[:DETAIL_LIMIT] if DETAIL_LIMIT else contracts)
    ids = [scraper.contract_id(c) for c in targets if scraper.contract_id(c)]
    results = {}
    pending = ids
    print(f"[{u}] INICIO DETALHAMENTO_HTTP_PARALLELO: {len(ids)} de {len(contracts)} workers={DETAIL_HTTP_WORKERS}")
    if DETAIL_LIMIT:
        print(f"[{u}] LIMITE_CONTROLADO_DETALHE: {DETAIL_LIMIT}")

    for attempt in range(1, DETAIL_RETRIES + 2):
        if not pending:
            break
        print(f"[{u}] RODADA_HTTP={attempt} pendentes={len(pending)}")
        next_pending = []
        with ThreadPoolExecutor(max_workers=DETAIL_HTTP_WORKERS) as pool:
            futures = {
                pool.submit(_http_one, cid, u, reps, storage_state): cid
                for cid in pending
            }
            for idx, future in enumerate(as_completed(futures), 1):
                cid = futures[future]
                try:
                    aluno = future.result()
                    if aluno:
                        results[str(cid)] = aluno
                    else:
                        raise RuntimeError("CONTRATO_SEM_RESULTADO")
                except Exception as exc:
                    next_pending.append(str(cid))
                    print(f"[{u}] HTTP_FALHA cid={cid}: {exc!r}")
                if idx % max(1, DETAIL_HTTP_WORKERS * 10) == 0 or idx == len(futures):
                    print(f"[{u}] PROGRESSO_HTTP: {idx}/{len(futures)} sucesso={len(results)} pendentes={len(next_pending)}")
        pending = list(dict.fromkeys(next_pending))

    if pending:
        print(f"[{u}] FALLBACK_EDGE: {len(pending)} contratos")
        edge_results, edge_pending = scraper_runner._edge_contracts(u, cfg, pending, reps, storage_state)
        for aluno in edge_results:
            results[str(aluno.get("contrato"))] = aluno
        pending = edge_pending

    ordered = [results[str(cid)] for cid in ids if str(cid) in results]
    print(f"[{u}] DETALHAMENTO_FINALIZADO: sucesso={len(ordered)} falhas={len(pending)} de_processados={len(ids)} total_disponivel={len(contracts)}")
    for cid in pending:
        print(f"[{u}] CONTRATO_NAO_CAPTURADO: {scraper.contract_url(cid)}")
    return ordered


scraper.process_details = _http_process


if __name__ == "__main__":
    mp.freeze_support()
    scraper.main()
