"""Runner CGD: listagem HTTP direta e detalhamento isolado por contrato com timeout real."""

import multiprocessing as mp
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
import scraper
from playwright.sync_api import sync_playwright

LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "831")))
LISTING_HTTP_WORKERS = max(1, int(os.getenv("CGD_LISTING_HTTP_WORKERS", "4")))
LISTING_TIMEOUT_S = max(5, int(os.getenv("CGD_LISTING_TIMEOUT_S", "30")))
DETAIL_TIMEOUT_S = max(30, int(os.getenv("CGD_DETAIL_TIMEOUT_S", "120")))
DETAIL_RETRIES = max(0, int(os.getenv("CGD_DETAIL_RETRIES", "1")))
MAX_CONTRACTS = scraper.MAX_CONTRACTS
LISTING_SOURCE = "https://app.cgd.com.br/alunos"


def _listing_source(page, destino):
    return LISTING_SOURCE


def _page_url(source, page_number):
    parsed = urlparse(source)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page_number)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _session_from_page(page):
    session = requests.Session()
    for cookie in page.context.cookies():
        session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/"))
    try:
        user_agent = page.evaluate("() => navigator.userAgent")
    except Exception:
        user_agent = None
    session.headers.update({"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8"})
    if user_agent:
        session.headers["User-Agent"] = user_agent
    return session


def _extract_contract_ids(html):
    return set(re.findall(r"/contratos/(\d+)", html or "", flags=re.IGNORECASE))


def _fetch_listing(args):
    unidade, url, cookies, headers = args
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(headers)
    response = session.get(url, timeout=LISTING_TIMEOUT_S, allow_redirects=True)
    final_path = urlparse(response.url).path.rstrip("/").lower()
    if final_path.startswith("/login") or "/login" in final_path:
        raise RuntimeError(f"sessao redirecionada para login: {url}")
    response.raise_for_status()
    return unidade, url, _extract_contract_ids(response.text), len(response.text)


def optimized_discover_contracts(page, unidade, destino):
    source = _listing_source(page, destino)
    print(f"[{unidade}] FONTE_LISTAGEM_FIXA: {source}")
    if not scraper.open_page(page, source, unidade, "lista_pagina_1", 300):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM: {source} final={page.url}")
    first_ids = _extract_contract_ids(page.content())
    print(f"[{unidade}] LISTAGEM REAL: {page.url} contratos_p1={len(first_ids)}")
    if not first_ids:
        raise RuntimeError(f"[{unidade}] LISTAGEM_PAGINA_1_SEM_CONTRATOS: {page.url}")
    session = _session_from_page(page)
    cookies = {c.name: c.value for c in session.cookies}
    headers = dict(session.headers)
    urls = [_page_url(source, n) for n in range(1, LISTING_PAGES + 1)]
    found = {cid: scraper.contract_url(cid) for cid in first_ids}
    print(f"[{unidade}] PAGINACAO_HTTP_DIRETA iniciado paginas=1..{LISTING_PAGES} workers={LISTING_HTTP_WORKERS} contratos_p1={len(first_ids)}")
    completed, errors = 1, 0
    with ThreadPoolExecutor(max_workers=LISTING_HTTP_WORKERS) as pool:
        futures = {pool.submit(_fetch_listing, (unidade, url, cookies, headers)): url for url in urls[1:]}
        for future in as_completed(futures):
            url = futures[future]
            try:
                _, _, ids, body_size = future.result()
                before = len(found)
                for cid in ids:
                    found[cid] = scraper.contract_url(cid)
                completed += 1
                page_number = parse_qs(urlparse(url).query).get("page", ["?"])[0]
                if completed % 10 == 0 or page_number == str(LISTING_PAGES):
                    print(f"[{unidade}] pagina_lista={page_number}/{LISTING_PAGES} contratos_acumulados={len(found)} novos={len(found)-before} bytes={body_size}")
            except Exception as exc:
                completed += 1
                errors += 1
                print(f"[{unidade}] pagina_lista_ERRO url={url}: {exc}")
    print(f"[{unidade}] PAGINACAO_HTTP_FINAL paginas={completed}/{LISTING_PAGES} contratos={len(found)} erros={errors}")
    if errors >= LISTING_PAGES // 2:
        raise RuntimeError(f"[{unidade}] LISTAGEM_HTTP_DEMASIADOS_ERROS: {errors}/{LISTING_PAGES}")
    return list(found.values())[:MAX_CONTRACTS]


def _detail_child(queue, u, cid, reps, storage_state):
    """Executa UM contrato em processo isolado. O processo pai pode encerrá-lo à força."""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context(storage_state=storage_state)
            page = context.new_page()
            try:
                # Proteção adicional: nenhum goto/espera individual pode exceder o limite global.
                page.set_default_timeout(30000)
                page.set_default_navigation_timeout(30000)
                aluno = scraper.contract_bundle(page, cid, u, reps)
                queue.put({"ok": True, "cid": cid, "aluno": aluno})
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        try:
            queue.put({"ok": False, "cid": cid, "error": repr(exc)})
        except Exception:
            pass


def _run_one_contract(u, cid, reps, storage_state):
    """Watchdog real: encerra o processo do contrato quando DETAIL_TIMEOUT_S expira."""
    ctx = mp.get_context("spawn")
    queue = ctx.Queue()
    proc = ctx.Process(target=_detail_child, args=(queue, u, cid, reps, storage_state), daemon=True)
    proc.start()
    proc.join(DETAIL_TIMEOUT_S)
    if proc.is_alive():
        print(f"[{u}] CONTRATO_TIMEOUT cid={cid} limite={DETAIL_TIMEOUT_S}s -> processo encerrado")
        proc.terminate()
        proc.join(10)
        if proc.is_alive() and hasattr(proc, "kill"):
            proc.kill()
            proc.join(5)
        queue.close()
        queue.join_thread()
        return {"ok": False, "cid": cid, "error": f"DETAIL_TIMEOUT_{DETAIL_TIMEOUT_S}s"}
    try:
        if not queue.empty():
            return queue.get_nowait()
    except Exception as exc:
        return {"ok": False, "cid": cid, "error": f"RESULTADO_INDISPONIVEL: {exc}"}
    return {"ok": False, "cid": cid, "error": f"PROCESSO_DETALHE_TERMINOU_SEM_RESULTADO exitcode={proc.exitcode}"}


def _persistent_detail_round(u, cfg, contracts, reps, storage_state, attempt):
    """Processa contratos sequencialmente com isolamento e watchdog real por contrato."""
    results, failed = [], []
    if not contracts:
        return results, failed
    print(f"[{u}] DETALHAMENTO_ISOLADO: {len(contracts)} contratos / watchdog={DETAIL_TIMEOUT_S}s / tentativa={attempt}")
    for index, contract in enumerate(contracts, 1):
        cid = scraper.contract_id(contract)
        if not cid:
            continue
        result = _run_one_contract(u, cid, reps, storage_state)
        if result.get("ok") and result.get("aluno"):
            results.append(result["aluno"])
            print(f"[{u}] CONTRATO_OK {index}/{len(contracts)} cid={cid}")
        else:
            failed.append(cid)
            print(f"[{u}] CONTRATO_ERRO {index}/{len(contracts)} cid={cid}: {result.get('error')}")
        if index % 5 == 0 or index == len(contracts):
            print(f"[{u}] PROGRESSO_DETALHAMENTO {index}/{len(contracts)} sucesso={len(results)} falhas={len(failed)}")
    return results, failed


def safe_process_details(u, cfg, contracts, reps, storage_state):
    if not contracts:
        return []
    pending = list(contracts)
    results = []
    for attempt in range(1, DETAIL_RETRIES + 2):
        if not pending:
            break
        print(f"[{u}] INICIO DETALHAMENTO: rodada={attempt} pendentes={len(pending)}")
        batch_results, failed_ids = _persistent_detail_round(u, cfg, pending, reps, storage_state, attempt)
        results.extend(batch_results)
        pending = [scraper.contract_url(cid) for cid in failed_ids if cid]
        if pending and attempt <= DETAIL_RETRIES:
            print(f"[{u}] RETENTATIVA: {len(pending)} contratos")
    print(f"[{u}] DETALHAMENTO FINALIZADO: sucesso={len(results)} falhas={len(pending)} de={len(contracts)}")
    for cu in pending:
        print(f"[{u}] CONTRATO_NAO_CAPTURADO: {cu}")
    return results


scraper.discover_contracts = optimized_discover_contracts
scraper.process_details = safe_process_details

if __name__ == "__main__":
    mp.freeze_support()
    scraper.main()
