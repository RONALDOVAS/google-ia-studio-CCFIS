"""Executor CGD: listagem HTTP rapida + detalhamento Edge na mesma sessao.

A listagem usa requests autenticado para percorrer as paginas sem abrir um
navegador por pagina. O detalhamento usa uma instancia Playwright dedicada,
sem criar sync_playwright dentro de outra instancia.
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
DETAIL_TIMEOUT_S = max(10, int(os.getenv("CGD_DETAIL_TIMEOUT_S", "120")))
DETAIL_RETRIES = max(0, int(os.getenv("CGD_DETAIL_RETRIES", "1")))
DETAIL_LIMIT = max(0, int(os.getenv("CGD_DETAIL_LIMIT", "0")))
HEADLESS = os.getenv("CGD_HEADLESS", "false").strip().lower() in {"1", "true", "yes", "on"}
MAX_CONTRACTS = scraper.MAX_CONTRACTS
LISTING_SOURCE = "https://app.cgd.com.br/alunos"


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


def _strict_open_page(page, url, unidade, name, wait=None):
    page.goto(url, wait_until="domcontentloaded", timeout=DETAIL_TIMEOUT_S * 1000)
    # O CGD entrega algumas telas imediatamente e outras terminam de montar
    # a tabela apos DOMContentLoaded. Sem esta espera, a pagina de frequencia
    # pode estar visualmente aberta, mas ainda sem as linhas que o parser le.
    stable_wait = max(1000, int(getattr(scraper, "PAGE_WAIT_MS", 500)))
    if wait is not None:
        stable_wait = max(stable_wait, int(wait))
    page.wait_for_timeout(stable_wait)
    try:
        page.wait_for_load_state("networkidle", timeout=15000)
    except Exception:
        pass
    path = urlparse(page.url).path.rstrip("/").lower()
    if path == "/login" or path.startswith("/login/"):
        raise SessionExpired(f"[{unidade}] SESSAO_EXPIRADA_NO_DETALHE etapa={name} url={url} final={page.url}")
    if urlparse(page.url).netloc != urlparse(scraper.CGD_URL).netloc:
        raise RuntimeError(f"[{unidade}] DETALHE_SAIU_DO_HOST etapa={name}: {page.url}")
    print(f"[{unidade}] {name}: {page.url}")
    try:
        scraper.dump(page, unidade, name)
    except Exception:
        pass
    return True


def _validate_real_detail(aluno, cid, unidade):
    if not aluno:
        raise RuntimeError(f"[{unidade}] CONTRATO_SEM_RESULTADO cid={cid}")
    nome = str(aluno.get("nome") or "").strip()
    if not nome or nome == f"Contrato {cid}":
        raise RuntimeError(f"[{unidade}] ALUNO_NAO_IDENTIFICADO cid={cid}")
    if not (aluno.get("frequencia_raw") or []):
        raise RuntimeError(f"[{unidade}] FREQUENCIA_NAO_CAPTURADA cid={cid}")
    return aluno


def process_details_with_browser(pw, unidade, cfg, contracts, reps):
    if not contracts:
        return []
    targets = list(contracts[:DETAIL_LIMIT] if DETAIL_LIMIT else contracts)
    print(f"[{unidade}] INICIO DETALHAMENTO_EDGE_PERSISTENTE: contratos={len(targets)} de={len(contracts)}")
    if DETAIL_LIMIT:
        print(f"[{unidade}] LIMITE_CONTROLADO_DETALHE: {DETAIL_LIMIT}")

    browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
    context = browser.new_context()
    page = context.new_page()
    previous_open_page = scraper.open_page
    scraper.open_page = _strict_open_page
    results = []
    pending = list(targets)

    try:
        for attempt in range(1, DETAIL_RETRIES + 2):
            if not pending:
                break
            print(f"[{unidade}] RODADA_DETALHE_EDGE={attempt} pendentes={len(pending)}")
            pending_next = []
            for idx, contract in enumerate(pending, 1):
                cid = scraper.contract_id(contract)
                if not cid:
                    print(f"[{unidade}] CONTRATO_INVALIDO: {contract}")
                    continue
                try:
                    print(f"[{unidade}] >>> PROCESSANDO CONTRATO {cid}")
                    aluno = scraper.contract_bundle(page, cid, unidade, reps)
                    aluno = _validate_real_detail(aluno, cid, unidade)
                    results.append(aluno)
                    print(f"[{unidade}] CONTRATO_OK {idx}/{len(pending)} cid={cid} nome={aluno.get('nome')} faltas={aluno.get('faltas')} presencas={aluno.get('presencas')} freq_registros={len(aluno.get('frequencia_raw') or [])}")
                except SessionExpired as exc:
                    print(f"[{unidade}] SESSAO_EXPIRADA cid={cid}: {exc}")
                    try:
                        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
                        aluno = scraper.contract_bundle(page, cid, unidade, reps)
                        aluno = _validate_real_detail(aluno, cid, unidade)
                        results.append(aluno)
                        print(f"[{unidade}] CONTRATO_OK_APOS_RELOGIN cid={cid} nome={aluno.get('nome')} faltas={aluno.get('faltas')} presencas={aluno.get('presencas')}")
                    except Exception as exc2:
                        pending_next.append(cid)
                        print(f"[{unidade}] CONTRATO_ERRO_APOS_RELOGIN cid={cid}: {exc2!r}")
                except Exception as exc:
                    pending_next.append(cid)
                    print(f"[{unidade}] CONTRATO_ERRO cid={cid}: {exc!r}")
                if idx % 10 == 0 or idx == len(pending):
                    print(f"[{unidade}] PROGRESSO_DETALHAMENTO_EDGE {idx}/{len(pending)} sucesso_total={len(results)} falhas_rodada={len(pending_next)}")
            pending = [scraper.contract_url(cid) for cid in pending_next if cid]
    finally:
        scraper.open_page = previous_open_page
        context.close()
        browser.close()

    print(f"[{unidade}] DETALHAMENTO_EDGE_FINALIZADO: sucesso={len(results)} falhas={len(pending)} de_processados={len(targets)} total_disponivel={len(contracts)}")
    for contract in pending:
        print(f"[{unidade}] CONTRATO_NAO_CAPTURADO: {contract}")
    return results


def run_unit(unidade, cfg, pw):
    browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
    context = browser.new_context()
    page = context.new_page()
    try:
        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
        contracts = optimized_discover_contracts(page, unidade, cfg["destino"])
        reps = scraper.get_replacements(page, unidade)
        print(f"[{unidade}] REPOSICOES GLOBAIS CAPTURADAS: {len(reps)}")
    except Exception as exc:
        print(f"[{unidade}] ERRO FATAL: {exc!r}")
        context.close(); browser.close()
        raise
    finally:
        context.close(); browser.close()
    return process_details_with_browser(pw, unidade, cfg, contracts, reps)


def main():
    print("=" * 80)
    print("SCRAPER CGD - COLETA REAL COMPLETA POR UNIDADE / ALUNO")
    print("Fluxo: autenticacao real -> listagem HTTP -> reposicoes -> Edge persistente")
    print(f"Configuracao: listing_workers={LISTING_HTTP_WORKERS}, detail_limit={DETAIL_LIMIT}, timeout_s={DETAIL_TIMEOUT_S}, diagnostico={scraper.DIAGNOSTICO}")
    print("=" * 80)
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
