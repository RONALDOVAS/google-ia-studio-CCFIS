"""Runner CGD: listagem HTTP direta e detalhamento protegido com baixo consumo de recursos."""

import multiprocessing as mp
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
import scraper

LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "829")))
LISTING_HTTP_WORKERS = max(1, int(os.getenv("CGD_LISTING_HTTP_WORKERS", "4")))
LISTING_TIMEOUT_S = max(5, int(os.getenv("CGD_LISTING_TIMEOUT_S", "30")))
DETAIL_WORKERS = max(1, int(os.getenv("CGD_DETAIL_WORKERS", "1")))
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


def _detail_process_entry(args, queue):
    try:
        queue.put(scraper.detail_worker(args))
    except BaseException as exc:
        queue.put({"ok": False, "cid": args[2], "error": repr(exc), "attempt": args[5]})


def _run_detail_batch(u, cfg, contracts, reps, storage_state, attempt):
    ctx = mp.get_context("spawn")
    results, failed = [], []
    total = len(contracts)
    workers = min(DETAIL_WORKERS, total)
    print(f"[{u}] BATCH {attempt}: {total} contratos / {workers} processos / timeout={DETAIL_TIMEOUT_S}s")
    for start in range(0, total, workers):
        chunk = contracts[start:start + workers]
        queue = ctx.Queue()
        running, started_at = [], {}
        for cu in chunk:
            cid = scraper.contract_id(cu)
            proc = ctx.Process(target=_detail_process_entry, args=((u, cfg, cid, reps, storage_state, attempt), queue))
            proc.start()
            running.append((proc, cid))
            started_at[cid] = time.monotonic()
        pending = {cid: proc for proc, cid in running}
        while pending:
            try:
                result = queue.get(timeout=1)
                cid = result.get("cid")
                proc = pending.pop(cid, None)
                if proc is None:
                    continue
                if result.get("ok"):
                    results.append(result["aluno"])
                else:
                    failed.append(cid)
                    print(f"[{u}] FALHA CONTRATO {cid}: {result.get('error', 'erro desconhecido')}")
                proc.join(timeout=2)
                if proc.is_alive():
                    proc.terminate()
                    proc.join(timeout=2)
            except Exception:
                pass
            now = time.monotonic()
            for cid, proc in list(pending.items()):
                if now - started_at[cid] >= DETAIL_TIMEOUT_S:
                    print(f"[{u}] TIMEOUT CONTRATO {cid} apos {DETAIL_TIMEOUT_S}s; encerrando processo")
                    if proc.is_alive():
                        proc.terminate()
                    proc.join(timeout=5)
                    failed.append(cid)
                    pending.pop(cid, None)
        for proc, _ in running:
            if proc.is_alive():
                proc.terminate()
            proc.join(timeout=2)
        try:
            queue.close(); queue.join_thread()
        except Exception:
            pass
        print(f"[{u}] PROGRESSO DETALHAMENTO: {min(start + workers, total)}/{total} contratos processados")
    return results, failed


def safe_process_details(u, cfg, contracts, reps, storage_state):
    if not contracts:
        return []
    pending, results = list(contracts), []
    for attempt in range(1, DETAIL_RETRIES + 2):
        if not pending:
            break
        print(f"[{u}] INICIO DETALHAMENTO PROTEGIDO: rodada={attempt} pendentes={len(pending)} workers={DETAIL_WORKERS}")
        batch_results, failed_ids = _run_detail_batch(u, cfg, pending, reps, storage_state, attempt)
        results.extend(batch_results)
        pending = [scraper.contract_url(cid) for cid in failed_ids if cid]
        if pending and attempt <= DETAIL_RETRIES:
            print(f"[{u}] RETENTATIVA: {len(pending)} contratos")
    print(f"[{u}] DETALHAMENTO FINALIZADO: sucesso={len(results)} falhas={len(pending)} de={len(contracts)}")
    for cu in pending:
        print(f"[{u}] CONTRATO NAO CAPTURADO: {cu}")
    return results


scraper.discover_contracts = optimized_discover_contracts
scraper.process_details = safe_process_details

if __name__ == "__main__":
    scraper.main()
