"""Executor de detalhes em thread isolada, preservando a captura funcional do CGD."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing as mp

import scraper
import scraper_runner


_original_contract_bundle = scraper.contract_bundle


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


# O fluxo de navegação/captura continua sendo o mesmo do Run #171.
scraper.contract_bundle = _contract_bundle_preservado


def _run_details_in_thread(u, cfg, contracts, reps, storage_state):
    return scraper_runner.safe_process_details(u, cfg, contracts, reps, storage_state)


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
