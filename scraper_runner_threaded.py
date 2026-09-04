"""Executa o runner de detalhes fora do loop asyncio do scraper principal."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing as mp

import scraper
import scraper_runner


def _run_details_in_thread(u, cfg, contracts, reps, storage_state):
    return scraper_runner.safe_process_details(u, cfg, contracts, reps, storage_state)


def threaded_process_details(u, cfg, contracts, reps, storage_state):
    # scraper.main() mantém um contexto Playwright Sync aberto no thread principal.
    # O runner de detalhes usa outro contexto Sync e, por isso, precisa rodar em
    # uma thread sem loop asyncio para evitar o erro "Sync API inside asyncio loop".
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
