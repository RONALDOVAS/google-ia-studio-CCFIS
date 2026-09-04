"""Executor CGD: listagem HTTP e detalhamento em sessao Edge persistente.

A autenticacao continua sendo feita uma vez pelo Edge. A listagem usa HTTP com
os cookies autenticados. O detalhamento usa UMA unica sessao Edge persistente,
porque as telas de curso/horarios/frequencia podem depender de JavaScript e
a abertura de um navegador/processo por aluno provocava perda de sessao.
"""

import json
import multiprocessing as mp
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
import scraper

LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "831")))
LISTING_HTTP_WORKERS = max(1, int(os.getenv("CGD_LISTING_HTTP_WORKERS", "4")))
LISTING_TIMEOUT_S = max(5, int(os.getenv("CGD_LISTING_TIMEOUT_S", "30")))
DETAIL_HTTP_WORKERS = max(1, int(os.getenv("CGD_DETAIL_HTTP_WORKERS", os.getenv("CGD_DETAIL_WORKERS", "4"))))
DETAIL_TIMEOUT_S = max(10, int(os.getenv("CGD_DETAIL_TIMEOUT_S", "60")))
DETAIL_RETRIES = max(0, int(os.getenv("CGD_DETAIL_RETRIES", "1")))
DETAIL_LIMIT = max(0, int(os.getenv("CGD_DETAIL_LIMIT", "0")))
MAX_CONTRACTS = scraper.MAX_CONTRACTS
LISTING_SOURCE = "https://app.cgd.com.br/alunos"


class SessionExpired(RuntimeError):
    pass


class _Element:
    def __init__(self, node):
        self.node = node

    def get_attribute(self, name):
        return self.node.get(name)

    def inner_text(self):
        return self.node.get_text(" ", strip=True)

    def all_text_contents(self):
        return [self.node.get_text(" ", strip=True)]

    def input_value(self):
        return self.node.get("value", "")

    def is_visible(self):
        return True

    def locator(self, selector):
        return _Locator(self.node.select(selector))


class _Locator:
    def __init__(self, nodes):
        self.nodes = list(nodes)

    def count(self):
        return len(self.nodes)

    def nth(self, index):
        return _Element(self.nodes[index])

    def all_text_contents(self):
        return [n.get_text(" ", strip=True) for n in self.nodes]

    def locator(self, selector):
        nodes = []
        for node in self.nodes:
            nodes.extend(node.select(selector))
        return _Locator(nodes)


class HTTPPage:
    """Adaptador minimo para reutilizar funcoes de extracao no modo HTTP."""

    def __init__(self, session):
        self.session = session
        self.url = ""
        self.html = ""
        self.soup = BeautifulSoup("<html><body></body></html>", "html.parser")
        self.last_status = None

    def goto(self, url, wait_until="domcontentloaded", timeout=30000):
        timeout_s = max(1, int(timeout / 1000))
        response = self.session.get(url, timeout=timeout_s, allow_redirects=True)
        self.last_status = response.status_code
        self.url = response.url
        self.html = response.text
        self.soup = BeautifulSoup(self.html, "html.parser")
        if response.status_code >= 400:
            raise RuntimeError(f"HTTP_{response.status_code}: {url} -> {response.url}")
        return response

    def wait_for_timeout(self, _ms):
        return None

    def content(self):
        return self.html

    def locator(self, selector):
        return _Locator(self.soup.select(selector))

    def screenshot(self, **_kwargs):
        raise RuntimeError("screenshot nao disponivel no modo HTTP")


def _session_from_storage_state(storage_state_path):
    with open(storage_state_path, "r", encoding="utf-8") as fh:
        state = json.load(fh)
    session = requests.Session()
    for cookie in state.get("cookies", []):
        try:
            session.cookies.set(cookie["name"], cookie["value"], domain=cookie.get("domain"), path=cookie.get("path", "/"))
        except Exception:
            session.cookies.set(cookie["name"], cookie["value"])
    session.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        "Cache-Control": "no-cache",
        "Pragma": "no-cache",
    })
    return session


def _session_headers_from_browser(page):
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
    })
    return session


def _page_url(source, page_number):
    parsed = urlparse(source)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page_number)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


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
        raise SessionExpired(f"LISTAGEM_SESSAO_EXPIRADA: {url} -> {response.url}")
    response.raise_for_status()
    return unidade, url, _extract_contract_ids(response.text), len(response.text)


def optimized_discover_contracts(page, unidade, destino):
    source = LISTING_SOURCE
    print(f"[{unidade}] FONTE_LISTAGEM_FIXA: {source}")
    if not scraper.open_page(page, source, unidade, "lista_pagina_1", 300):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM: {source} final={page.url}")
    first_ids = _extract_contract_ids(page.content())
    print(f"[{unidade}] LISTAGEM REAL: {page.url} contratos_p1={len(first_ids)}")
    if not first_ids:
        raise RuntimeError(f"[{unidade}] LISTAGEM_PAGINA_1_SEM_CONTRATOS: {page.url}")
    session = _session_headers_from_browser(page)
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


def _strict_open_page(page, url, u, name, wait=None):
    page.goto(url, wait_until="domcontentloaded", timeout=DETAIL_TIMEOUT_S * 1000)
    page.wait_for_timeout(wait or 0)
    path = urlparse(page.url).path.rstrip("/").lower()
    if path == "/login" or path.startswith("/login/"):
        raise SessionExpired(f"[{u}] SESSAO_EXPIRADA_NO_DETALHE etapa={name} url={url} final={page.url}")
    if urlparse(page.url).netloc != urlparse(scraper.CGD_URL).netloc:
        raise RuntimeError(f"[{u}] DETALHE_SAIU_DO_HOST etapa={name}: {page.url}")
    print(f"[{u}] {name}: {page.url}")
    try:
        scraper.dump(page, u, name)
    except Exception:
        pass
    return True


def _http_contract_bundle(session, cid, u, reps):
    """Mantido apenas como fallback tecnico; o detalhe principal usa Edge."""
    page = HTTPPage(session)
    aluno = scraper.contract_bundle(page, cid, u, reps)
    if not aluno:
        raise RuntimeError(f"[{u}] CONTRATO_SEM_RESULTADO cid={cid}")
    return aluno


def _validate_real_detail(aluno, cid, u):
    if not aluno:
        raise RuntimeError(f"[{u}] CONTRATO_SEM_RESULTADO cid={cid}")
    nome = str(aluno.get("nome") or "").strip()
    if not nome or nome == f"Contrato {cid}":
        raise RuntimeError(f"[{u}] ALUNO_NAO_IDENTIFICADO cid={cid}")
    freq = aluno.get("frequencia_raw") or []
    if not freq:
        raise RuntimeError(f"[{u}] FREQUENCIA_NAO_CAPTURADA cid={cid}")
    return aluno


def safe_process_details(u, cfg, contracts, reps, storage_state):
    if not contracts:
        return []
    targets = list(contracts[:DETAIL_LIMIT] if DETAIL_LIMIT else contracts)
    print(f"[{u}] INICIO DETALHAMENTO_EDGE_PERSISTENTE: contratos={len(targets)} de={len(contracts)}")
    if DETAIL_LIMIT:
        print(f"[{u}] LIMITE_CONTROLADO_DETALHE: {DETAIL_LIMIT}")
    results = []
    pending = list(targets)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
        context = browser.new_context(storage_state=storage_state)
        page = context.new_page()
        previous_open_page = scraper.open_page
        scraper.open_page = _strict_open_page
        try:
            if not scraper.same_host(page.url) and page.url == "about:blank":
                pass
            for attempt in range(1, DETAIL_RETRIES + 2):
                if not pending:
                    break
                print(f"[{u}] RODADA_DETALHE_EDGE={attempt} pendentes={len(pending)}")
                pending_next = []
                for idx, contract in enumerate(pending, 1):
                    cid = scraper.contract_id(contract)
                    if not cid:
                        print(f"[{u}] CONTRATO_INVALIDO: {contract}")
                        continue
                    try:
                        print(f"[{u}] >>> PROCESSANDO CONTRATO {cid}")
                        aluno = scraper.contract_bundle(page, cid, u, reps)
                        aluno = _validate_real_detail(aluno, cid, u)
                        results.append(aluno)
                        print(f"[{u}] CONTRATO_OK {idx}/{len(pending)} cid={cid} nome={aluno.get('nome')} faltas={aluno.get('faltas')} presencas={aluno.get('presencas')} freq_registros={len(aluno.get('frequencia_raw') or [])}")
                    except SessionExpired as exc:
                        print(f"[{u}] SESSAO_EXPIRADA cid={cid}: {exc}")
                        try:
                            scraper.login(page, cfg["usuario"], cfg["senha"], u)
                            print(f"[{u}] RELOGIN_OK antes de repetir cid={cid}")
                            aluno = scraper.contract_bundle(page, cid, u, reps)
                            aluno = _validate_real_detail(aluno, cid, u)
                            results.append(aluno)
                            print(f"[{u}] CONTRATO_OK_APOS_RELOGIN cid={cid} nome={aluno.get('nome')} faltas={aluno.get('faltas')} presencas={aluno.get('presencas')}")
                        except Exception as exc2:
                            pending_next.append(cid)
                            print(f"[{u}] CONTRATO_ERRO_APOS_RELOGIN cid={cid}: {exc2!r}")
                            try:
                                scraper.dump(page, u, f"falha_{cid}")
                            except Exception:
                                pass
                    except Exception as exc:
                        pending_next.append(cid)
                        print(f"[{u}] CONTRATO_ERRO cid={cid}: {exc!r}")
                        try:
                            scraper.dump(page, u, f"falha_{cid}")
                        except Exception:
                            pass
                    if idx % 10 == 0 or idx == len(pending):
                        print(f"[{u}] PROGRESSO_DETALHAMENTO_EDGE {idx}/{len(pending)} sucesso_total={len(results)} falhas_rodada={len(pending_next)}")
                pending = [scraper.contract_url(cid) for cid in pending_next if cid]
        finally:
            scraper.open_page = previous_open_page
            context.close()
            browser.close()
    print(f"[{u}] DETALHAMENTO_EDGE_FINALIZADO: sucesso={len(results)} falhas={len(pending)} de_processados={len(targets)} total_disponivel={len(contracts)}")
    for cu in pending:
        print(f"[{u}] CONTRATO_NAO_CAPTURADO: {cu}")
    return results


scraper.discover_contracts = optimized_discover_contracts
scraper.process_details = safe_process_details


if __name__ == "__main__":
    mp.freeze_support()
    scraper.main()
