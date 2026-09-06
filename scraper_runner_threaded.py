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
_CF_MARKERS = (
    "verifying you are human",
    "just a moment",
    "checking your browser",
    "cf-chl-",
    "challenge-platform",
)


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


def _pagina_protegida(page):
    try:
        url = (page.url or "").lower()
        texto = scraper.low(page.locator("body").inner_text())[:20000]
        return "/cdn-cgi/challenge" in url or any(marker in texto for marker in _CF_MARKERS)
    except Exception:
        return False


def _preflight_sessao(page, unidade):
    """Valida a sessão autenticada/Cloudflare antes de consumir contratos."""
    page.goto(scraper.CGD_URL, wait_until="domcontentloaded", timeout=scraper_runner.DETAIL_TIMEOUT_S * 1000)
    for tentativa in range(1, 7):
        page.wait_for_timeout(1000)
        if not _pagina_protegida(page):
            cookies = {c["name"] for c in page.context.cookies(scraper.CGD_URL)}
            cf = "cf_clearance" in cookies
            bm = "__cf_bm" in cookies
            print(f"[{unidade}] SESSAO_CGD_VALIDADA tentativa={tentativa} cf_clearance={cf} cf_bm={bm} url={page.url}", flush=True)
            return
        print(f"[{unidade}] CLOUDFLARE_AINDA_PRESENTE tentativa={tentativa} url={page.url}", flush=True)
        page.reload(wait_until="domcontentloaded", timeout=scraper_runner.DETAIL_TIMEOUT_S * 1000)
    raise RuntimeError(f"[{unidade}] CLOUDFLARE_SESSAO_NAO_VALIDADA: a sessão autenticada não chegou ao CGD real")


def _frequencia_com_espera(page, cid):
    """Não aceita uma página vazia como frequência válida: aguarda o DOM/AJAX real."""
    ultimo_erro = None
    for tentativa, espera in enumerate((0, 750, 1500, 2500, 4000), 1):
        if espera:
            page.wait_for_timeout(espera)
        try:
            resultado = scraper_runner.robust_extract_frequency(page, cid)
            if resultado.get("registros"):
                print(f"[FREQUENCIA_VALIDADA] cid={cid} tentativa={tentativa} registros={len(resultado['registros'])} faltas={resultado.get('faltas', 0)} presencas={resultado.get('presencas', 0)}", flush=True)
                return resultado
            ultimo_erro = RuntimeError("parser retornou zero registros")
        except Exception as exc:
            ultimo_erro = exc
        if tentativa < 5:
            print(f"[FREQUENCIA_AGUARDANDO] cid={cid} tentativa={tentativa} aguardando conteúdo dinâmico", flush=True)
    raise RuntimeError(f"FREQUENCIA_NAO_CAPTURADA_APOS_ESPERA cid={cid}: {ultimo_erro}")


scraper.extract_frequency = _frequencia_com_espera


def _run_details_in_thread(u, cfg, contracts, reps, storage_state):
    """Playwright Sync exclusivamente nesta thread, com a sessão autenticada restaurada."""
    return scraper_runner.safe_process_details(u, cfg, contracts, reps, storage_state)


def _persistent_detail_round_validated(u, cfg, contracts, reps, storage_state, attempt):
    results, failed = [], []
    if not contracts:
        return results, failed

    scraper_runner.progress.set_total(6 if os.getenv("CGD_DIAGNOSTICO", "0").lower() in ("1", "true", "yes", "sim") else len(contracts) * 2)
    headless = os.getenv("CGD_HEADLESS", "0").lower() in ("1", "true", "yes", "sim")
    print(f"[{u}] DETALHAMENTO VALIDADO: {len(contracts)} contratos | Edge | headless={headless}", flush=True)

    with scraper_runner.sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=headless)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()
        try:
            _preflight_sessao(page, u)
            for index, contract in enumerate(contracts, 1):
                cid = scraper.contract_id(contract)
                if not cid:
                    continue
                try:
                    print(f"[{u}] CONTRATO_INICIO {index}/{len(contracts)} cid={cid}", flush=True)
                    result = scraper.contract_bundle(page, cid, u, reps)
                    if not result:
                        raise RuntimeError(f"CONTRATO_SEM_DADOS cid={cid}")
                    if not (result.get("frequencia_raw") or []):
                        raise RuntimeError(f"CONTRATO_SEM_FREQUENCIA cid={cid}")
                    results.append(result)
                    scraper_runner.progress.update(True, f"{u} contrato={cid}")
                    print(f"[{u}] CONTRATO_OK {index}/{len(contracts)} cid={cid} nome={result.get('nome')} faltas={result.get('faltas')} presencas={result.get('presencas')} freq_registros={len(result.get('frequencia_raw') or [])}", flush=True)
                except Exception as exc:
                    failed.append(cid)
                    scraper_runner.progress.update(False, f"{u} contrato={cid} FALHA")
                    print(f"[{u}] CONTRATO_ERRO {index}/{len(contracts)} cid={cid}: {exc!r}", flush=True)
        finally:
            context.close()
            browser.close()
    return results, failed


# O safe_process_details original chama esta função por nome; substituímos somente
# a rodada de detalhes para preservar listagem, autenticação e armazenamento existentes.
scraper_runner._persistent_detail_round = _persistent_detail_round_validated


def threaded_process_details(u, cfg, contracts, reps, storage_state):
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="cgd-detail") as pool:
        return pool.submit(_run_details_in_thread, u, cfg, contracts, reps, storage_state).result()


scraper.discover_contracts = scraper_runner.optimized_discover_contracts
scraper.process_details = threaded_process_details


if __name__ == "__main__":
    mp.freeze_support()
    scraper.main()
