"""Executor CGD: listagem HTTP rapida + detalhamento paralelo em lotes.

A listagem usa requests autenticado para percorrer as paginas sem abrir um
navegador por pagina. O detalhamento usa varios workers Edge independentes,
mas cada worker mantem um navegador aberto para processar varios contratos.
Isso evita abrir/fechar um Edge inteiro para cada aluno.
"""

import os
import re
import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from playwright.sync_api import sync_playwright
import scraper

LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "831")))
LISTING_HTTP_WORKERS = max(1, int(os.getenv("CGD_LISTING_HTTP_WORKERS", "4")))
LISTING_TIMEOUT_S = max(5, int(os.getenv("CGD_LISTING_TIMEOUT_S", "30")))
DETAIL_LIMIT = max(0, int(os.getenv("CGD_DETAIL_LIMIT", "0")))
MAX_CONTRACTS = scraper.MAX_CONTRACTS
LISTING_SOURCE = "https://app.cgd.com.br/alunos"
HEADLESS = os.getenv("CGD_HEADLESS", "false").strip().lower() in {"1", "true", "yes", "on"}


class SessionExpired(RuntimeError):
    pass


def _page_url(source, page_number):
    parsed = urlparse(source)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page_number)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _extract_contract_ids(html):
    return set(re.findall(r"/contratos/(\d+)", html or "", flags=re.IGNORECASE))


def _session_from_browser(page):
    session = requests.Session()
    for cookie in page.context.cookies():
        try:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/"))
        except Exception:
            session.cookies.set(cookie["name"], cookie["value"])
    try:
        session.headers["User-Agent"] = page.evaluate("() => navigator.userAgent")
    except Exception:
        pass
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return session


def _fetch_listing(args):
    unidade, url, cookies, headers = args
    session = requests.Session()
    session.cookies.update(cookies)
    session.headers.update(headers)
    response = session.get(url, timeout=LISTING_TIMEOUT_S, allow_redirects=True)
    path = urlparse(response.url).path.rstrip("/").lower()
    if path == "/login" or path.startswith("/login/"):
        raise SessionExpired(f"[{unidade}] LISTAGEM_SESSAO_EXPIRADA: {url} -> {response.url}")
    response.raise_for_status()
    return url, _extract_contract_ids(response.text), len(response.text)


def optimized_discover_contracts(page, unidade, destino):
    source = LISTING_SOURCE
    print(f"[{unidade}] FONTE_LISTAGEM_FIXA: {source}")
    if not scraper.open_page(page, source, unidade, "lista_pagina_1", 300):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM: {source} final={page.url}")
    first_ids = _extract_contract_ids(page.content())
    print(f"[{unidade}] LISTAGEM REAL: {page.url} contratos_p1={len(first_ids)}")
    if not first_ids:
        raise RuntimeError(f"[{unidade}] LISTAGEM_PAGINA_1_SEM_CONTRATOS: {page.url}")

    session = _session_from_browser(page)
    cookies = {c.name: c.value for c in session.cookies}
    headers = dict(session.headers)
    urls = [_page_url(source, n) for n in range(1, LISTING_PAGES + 1)]
    found = {cid: scraper.contract_url(cid) for cid in first_ids}
    print(f"[{unidade}] PAGINACAO_HTTP_DIRETA iniciado paginas=1..{LISTING_PAGES} workers={LISTING_HTTP_WORKERS} contratos_p1={len(first_ids)}")

    completed = 1
    errors = 0
    with ThreadPoolExecutor(max_workers=LISTING_HTTP_WORKERS) as pool:
        futures = {pool.submit(_fetch_listing, (unidade, url, cookies, headers)): url for url in urls[1:]}
        for future in as_completed(futures):
            url = futures[future]
            try:
                _, ids, body_size = future.result()
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
    if errors >= max(1, LISTING_PAGES // 2):
        raise RuntimeError(f"[{unidade}] LISTAGEM_HTTP_DEMASIADOS_ERROS: {errors}/{LISTING_PAGES}")
    return list(found.values())[:MAX_CONTRACTS]


def _detail_batch(args):
    """Um worker abre um Edge uma unica vez e processa seu lote inteiro."""
    unidade, cfg, contracts, reps, storage_state, round_no, worker_no = args
    results = []
    failures = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()
        try:
            print(f"[{unidade}] WORKER_DETALHE {worker_no}: inicio lote={len(contracts)} tentativa={round_no}")
            for url in contracts:
                cid = scraper.contract_id(url)
                if not cid:
                    failures.append((url, "CONTRATO_ID_INVALIDO"))
                    continue
                try:
                    aluno = scraper.contract_bundle(page, cid, unidade, reps)
                    aluno = scraper.validate_real_detail(aluno, cid, unidade)
                    results.append(aluno)
                    print(f"[{unidade}] CONTRATO_OK cid={cid} nome={aluno.get('nome')} faltas={aluno.get('faltas')} presencas={aluno.get('presencas')} freq_registros={len(aluno.get('frequencia_raw') or [])}")
                except Exception as exc:
                    failures.append((url, repr(exc)))
                    print(f"[{unidade}] FALHA DETALHE cid={cid}: {exc!r}")
        finally:
            context.close()
            browser.close()
    return results, failures


def _make_batches(items, workers):
    workers = max(1, min(workers, len(items)))
    batches = [[] for _ in range(workers)]
    for i, item in enumerate(items):
        batches[i % workers].append(item)
    return [b for b in batches if b]


def process_details_fast(unidade, cfg, contracts, reps, storage_state):
    if not contracts:
        return []
    targets = list(contracts[:DETAIL_LIMIT] if DETAIL_LIMIT else contracts)
    workers = min(max(1, scraper.DETAIL_WORKERS), len(targets))
    print(f"[{unidade}] INICIO DETALHAMENTO RAPIDO: {len(targets)} contratos / {workers} Edge workers persistentes")
    if DETAIL_LIMIT:
        print(f"[{unidade}] LIMITE_CONTROLADO_DETALHE: {DETAIL_LIMIT}")

    pending = targets
    results = []
    for round_no in (1, 2):
        if not pending:
            break
        batches = _make_batches(pending, workers)
        print(f"[{unidade}] LOTE_DETALHE {round_no}: contratos={len(pending)} batches={len(batches)}")
        next_pending = []
        with ThreadPoolExecutor(max_workers=len(batches)) as pool:
            futures = [pool.submit(_detail_batch, (unidade, cfg, batch, reps, storage_state, round_no, i + 1)) for i, batch in enumerate(batches)]
            for fut in as_completed(futures):
                try:
                    ok, failed = fut.result()
                    results.extend(ok)
                    next_pending.extend(url for url, _ in failed)
                except Exception as exc:
                    print(f"[{unidade}] WORKER_DETALHE_ERRO: {exc!r}")
        print(f"[{unidade}] PROGRESSO DETALHAMENTO: sucesso_total={len(results)} falhas_para_retry={len(next_pending)}")
        pending = next_pending

    print(f"[{unidade}] DETALHAMENTO FINALIZADO: sucesso={len(results)} falhas={len(pending)} de={len(targets)}")
    for contract in pending:
        print(f"[{unidade}] CONTRATO_NAO_CAPTURADO: {contract}")
    return results


def run_unit(unidade, cfg, pw):
    profile = scraper.EDGE_PROFILE_BASE / unidade
    profile.mkdir(parents=True, exist_ok=True)
    browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
    context = browser.new_context()
    page = context.new_page()
    state = profile / "storage_state.json"
    try:
        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
        contracts = optimized_discover_contracts(page, unidade, cfg["destino"])
        reps = scraper.get_replacements(page, unidade)
        print(f"[{unidade}] REPOSICOES GLOBAIS CAPTURADAS: {len(reps)}")
        context.storage_state(path=str(state))
    except Exception as exc:
        print(f"[{unidade}] ERRO FATAL: {exc!r}")
        raise
    finally:
        context.close()
        browser.close()
    return process_details_fast(unidade, cfg, contracts, reps, str(state))


def main():
    print("=" * 80)
    print("SCRAPER CGD - COLETA REAL COMPLETA POR UNIDADE / ALUNO")
    print("Fluxo: autenticacao real -> listagem HTTP -> reposicoes -> detalhamento Edge em lotes paralelos")
    print(f"Configuracao: listing_workers={LISTING_HTTP_WORKERS}, detail_workers={scraper.DETAIL_WORKERS}, detail_limit={DETAIL_LIMIT}, page_wait_ms={scraper.PAGE_WAIT_MS}, timeout_ms={scraper.PAGE_TIMEOUT_MS}, diagnostico={scraper.DIAGNOSTICO}")
    print("=")
    all_alunos = []
    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            try:
                all_alunos += run_unit(unidade, scraper.CONFIG[unidade], pw)
            except Exception as exc:
                print(f"[{unidade}] UNIDADE_ABORTADA: {exc!r}")
    scraper.JSON_PATH.write_text(json.dumps(all_alunos, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 80)
    print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(all_alunos)}")
    print(f"MATRIZ: {sum(1 for a in all_alunos if a.get('unidade') == 'matriz')}")
    print(f"FILIAL: {sum(1 for a in all_alunos if a.get('unidade') == 'filial')}")
    print("=" * 80)


if __name__ == "__main__":
    main()
