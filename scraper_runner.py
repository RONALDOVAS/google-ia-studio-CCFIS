"""Runner CGD: listagem HTTP direta e detalhamento protegido com baixo consumo de recursos."""

import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
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
JSON_PATH = Path("dados_alunos.json")


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


def _existing_ids_for_unit(unidade):
    try:
        if not JSON_PATH.exists():
            return set()
        data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
        if not isinstance(data, list):
            return set()
        return {str(a.get("contrato") or a.get("cgd_matricula_id") or "").strip() for a in data if a.get("unidade") == unidade and (a.get("contrato") or a.get("cgd_matricula_id"))}
    except Exception as exc:
        print(f"[{unidade}] AVISO_LEITURA_INCREMENTAL_LISTAGEM: {exc!r}", flush=True)
        return set()


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
    print(f"[{unidade}] FONTE_LISTAGEM_FIXA: {source}", flush=True)
    if not scraper.open_page(page, source, unidade, "lista_pagina_1", 300):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM: {source} final={page.url}")
    first_ids = _extract_contract_ids(page.content())
    print(f"[{unidade}] LISTAGEM REAL: {page.url} contratos_p1={len(first_ids)}", flush=True)
    if not first_ids:
        raise RuntimeError(f"[{unidade}] LISTAGEM_PAGINA_1_SEM_CONTRATOS: {page.url}")

    existing_ids = _existing_ids_for_unit(unidade)
    alvo = max(1, int(os.getenv("CGD_DETAIL_TARGET_PER_UNIT", "3")))
    novos_necessarios = max(0, alvo - len(existing_ids))
    found = {cid: scraper.contract_url(cid) for cid in first_ids}
    novos_found = set(found) - existing_ids
    print(f"[{unidade}] PAGINACAO_INCREMENTAL: existentes={len(existing_ids)} alvo={alvo} novos_necessarios={novos_necessarios} novos_p1={len(novos_found)} limite_paginas={LISTING_PAGES}", flush=True)

    if len(novos_found) >= novos_necessarios:
        print(f"[{unidade}] PAGINACAO_INCREMENTAL_FINAL: somente_pagina_1_suficiente contratos={len(found)}", flush=True)
        return list(found.values())[:MAX_CONTRACTS]

    session = _session_from_page(page)
    cookies = {c.name: c.value for c in session.cookies}
    headers = dict(session.headers)
    completed, errors = 1, 0
    next_page = 2
    batch_size = max(LISTING_HTTP_WORKERS * 2, 8)
    print(f"[{unidade}] PAGINACAO_HTTP_INCREMENTAL iniciado paginas=2..{LISTING_PAGES} workers={LISTING_HTTP_WORKERS} lote={batch_size}; para assim que encontrar {novos_necessarios} novos contratos", flush=True)

    while next_page <= LISTING_PAGES and len(novos_found) < novos_necessarios:
        last_page = min(LISTING_PAGES, next_page + batch_size - 1)
        urls = [_page_url(source, n) for n in range(next_page, last_page + 1)]
        print(f"[{unidade}] LOTE_LISTAGEM paginas={next_page}-{last_page} novos_acumulados={len(novos_found)}/{novos_necessarios}", flush=True)
        with ThreadPoolExecutor(max_workers=LISTING_HTTP_WORKERS) as pool:
            futures = {pool.submit(_fetch_listing, (unidade, url, cookies, headers)): url for url in urls}
            for future in as_completed(futures):
                url = futures[future]
                try:
                    _, _, ids, body_size = future.result()
                    before = len(found)
                    for cid in ids:
                        found[cid] = scraper.contract_url(cid)
                    novos_found = set(found) - existing_ids
                    completed += 1
                    page_number = parse_qs(urlparse(url).query).get("page", ["?"])[0]
                    print(f"[{unidade}] pagina_lista={page_number}/{LISTING_PAGES} contratos_acumulados={len(found)} novos_acumulados={len(novos_found)}/{novos_necessarios} novos_pagina={len(found)-before} bytes={body_size}", flush=True)
                    if len(novos_found) >= novos_necessarios:
                        break
                except Exception as exc:
                    completed += 1
                    errors += 1
                    print(f"[{unidade}] pagina_lista_ERRO url={url}: {exc}", flush=True)
        next_page = last_page + 1

    print(f"[{unidade}] PAGINACAO_HTTP_FINAL paginas_processadas={completed} contratos={len(found)} novos={len(novos_found)}/{novos_necessarios} erros={errors}", flush=True)
    if errors >= max(1, completed // 2):
        raise RuntimeError(f"[{unidade}] LISTAGEM_HTTP_DEMASIADOS_ERROS: {errors}/{completed}")
    if len(novos_found) < novos_necessarios:
        print(f"[{unidade}] AVISO: listagem terminou antes de atingir alvo; usando todos os contratos novos encontrados", flush=True)
    return list(found.values())[:MAX_CONTRACTS]


def _persistent_detail_round(u, cfg, contracts, reps, storage_state, attempt):
    """Processa todos os contratos da unidade usando UM unico navegador Edge em modo headless."""
    results, failed = [], []
    if not contracts:
        return results, failed
    print(f"[{u}] DETALHAMENTO_PERSISTENTE: {len(contracts)} contratos / 1 Edge / timeout={DETAIL_TIMEOUT_S}s")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=True)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()
        try:
            for index, contract in enumerate(contracts, 1):
                cid = scraper.contract_id(contract)
                if not cid:
                    continue
                try:
                    result = scraper.contract_bundle(page, cid, u, reps)
                    results.append(result)
                    print(f"[{u}] CONTRATO_OK {index}/{len(contracts)} cid={cid}")
                except Exception as exc:
                    failed.append(cid)
                    print(f"[{u}] CONTRATO_ERRO {index}/{len(contracts)} cid={cid}: {exc}")
        finally:
            try:
                context.close()
            finally:
                browser.close()
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
    scraper.main()
