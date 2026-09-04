"""Executa o runner de detalhes fora do loop asyncio do scraper principal."""

from concurrent.futures import ThreadPoolExecutor
import multiprocessing as mp

import scraper
import scraper_runner


def fixed_extract_frequency(page, cid):
    """Extrai frequencia do CGD inclusive quando a situacao fica na coluna Aula.

    Em paginas reais do CGD, a tabela observada usa cabecalhos como:
    Data | Horario | Sala | Curso | Aula | Conteudo | ...
    e os valores "Presente" / "Faltou" aparecem dentro da coluna Aula.
    O parser original procurava uma coluna de status e, por isso, contava zero.
    """
    rec = []
    faltas = 0
    presencas = 0

    presente_tokens = {
        "presente",
        "compareceu",
        "presenca",
        "presenca registrada",
    }
    falta_tokens = {
        "faltou",
        "falta",
        "ausente",
        "nao compareceu",
        "não compareceu",
    }

    for heads, rows in scraper.table_data(page):
        si = scraper.col(
            heads,
            "status",
            "situação",
            "situacao",
            "presença",
            "presenca",
            "frequência",
            "frequencia",
        )
        di = scraper.col(heads, "data", "dia")
        ai = scraper.col(heads, "aluno", "nome", "estudante")

        for row in rows:
            status_index = si if si is not None and si < len(row) else None
            status = scraper.norm(row[status_index]) if status_index is not None else ""
            status_low = scraper.low(status)

            # Fallback real do CGD: "Presente"/"Faltou" pode estar em qualquer
            # celula da linha, tipicamente em "Aula".
            if not status_low:
                for cell in row:
                    candidate = scraper.low(cell)
                    if candidate in falta_tokens or candidate in presente_tokens:
                        status = scraper.norm(cell)
                        status_low = candidate
                        break

            if status_low in falta_tokens:
                faltas += 1
            elif status_low in presente_tokens:
                presencas += 1

            rec.append(
                {
                    "data": row[di] if di is not None and di < len(row) else None,
                    "status": status or None,
                    "aluno": row[ai] if ai is not None and ai < len(row) else None,
                    "valores": row,
                    "cabecalhos": heads,
                }
            )

    return {"faltas": faltas, "presencas": presencas, "registros": rec}


# Substitui somente o parser de frequencia antes de iniciar a coleta.
scraper.extract_frequency = fixed_extract_frequency


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
