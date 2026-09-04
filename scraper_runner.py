"""Runner CGD com descoberta de contratos por HTTP autenticado.

O navegador e usado pelo scraper.py somente para autenticar e estabelecer a
sessao. A listagem do CGD usa a paginacao real /alunos?page=N diretamente por
HTTP, em paralelo e sem abrir um navegador por pagina. A etapa de detalhes
continua sendo responsabilidade do scraper.py.
"""

import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import requests
import scraper

LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "829")))
LISTING_HTTP_WORKERS = max(1, int(os.getenv("CGD_LISTING_HTTP_WORKERS", "8")))
LISTING_TIMEOUT_S = max(5, int(os.getenv("CGD_LISTING_TIMEOUT_S", "30")))
MAX_CONTRACTS = scraper.MAX_CONTRACTS


def _listing_source(page, destino):
    """Retorna a rota real de listagem /alunos usada pelo CGD."""
    if destino and scraper.same_host(destino):
        path = urlparse(destino).path.rstrip("/").lower()
        if path in ("/alunos", "/relatorios/alunos", "/relatorios/individuais/alunos-curso"):
            return destino

    selectors = 'a[href*="/alunos"],a[href*="/relatorios/alunos"],a[href*="/relatorios/individuais/alunos-curso"]'
    try:
        loc = page.locator(selectors)
        total = min(loc.count(), 100)
        for i in range(total):
            href = loc.nth(i).get_attribute("href")
            if not href:
                continue
            href = scraper.abs_url(page, href)
            path = urlparse(href).path.rstrip("/").lower()
            if path in ("/alunos", "/relatorios/alunos", "/relatorios/individuais/alunos-curso"):
                return href
    except Exception:
        pass
    return None


def _page_url(source, page_number):
    """Monta exatamente /alunos?page=N, preservando os demais parametros."""
    parsed = urlparse(source)
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["page"] = [str(page_number)]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def _session_from_page(page):
    """Copia os cookies da sessao autenticada do Playwright para requests."""
    session = requests.Session()
    for cookie in page.context.cookies():
        session.cookies.set(
            cookie["name"],
            cookie["value"],
            domain=cookie.get("domain"),
            path=cookie.get("path", "/"),
        )

    try:
        user_agent = page.evaluate("() => navigator.userAgent")
    except Exception:
        user_agent = None

    session.headers.update(
        {
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
        }
    )
    if user_agent:
        session.headers["User-Agent"] = user_agent
    return session


def _extract_contract_ids(html):
    """Extrai somente IDs de contrato do HTML bruto, sem interpretar cores."""
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

    ids = _extract_contract_ids(response.text)
    return unidade, url, ids, len(response.text)


def optimized_discover_contracts(page, unidade, destino):
    """Descobre todos os contratos sem navegar pagina por pagina no navegador."""
    source = _listing_source(page, destino)
    if not source:
        raise RuntimeError(f"[{unidade}] LISTAGEM_NAO_ENCONTRADA")

    # Uma unica navegacao inicial valida que a sessao autenticada enxerga a lista.
    if not scraper.open_page(page, source, unidade, "lista_pagina_1", 300):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM")

    first_html = page.content()
    first_ids = _extract_contract_ids(first_html)
    if not first_ids:
        raise RuntimeError(f"[{unidade}] LISTAGEM_PAGINA_1_SEM_CONTRATOS")

    session = _session_from_page(page)
    cookies = {c.name: c.value for c in session.cookies}
    headers = dict(session.headers)

    urls = [_page_url(source, n) for n in range(1, LISTING_PAGES + 1)]
    found = {}
    for cid in first_ids:
        found[cid] = scraper.contract_url(cid)

    print(
        f"[{unidade}] PAGINACAO_HTTP_DIRETA iniciado paginas=1..{LISTING_PAGES} "
        f"workers={LISTING_HTTP_WORKERS} contratos_p1={len(first_ids)}"
    )

    completed = 1
    with ThreadPoolExecutor(max_workers=LISTING_HTTP_WORKERS) as pool:
        futures = {
            pool.submit(_fetch_listing, (unidade, url, cookies, headers)): url
            for url in urls[1:]
        }
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
                    print(
                        f"[{unidade}] pagina_lista={page_number}/{LISTING_PAGES} "
                        f"contratos_acumulados={len(found)} novos={len(found)-before} bytes={body_size}"
                    )
            except Exception as exc:
                completed += 1
                print(f"[{unidade}] pagina_lista_ERRO url={url}: {exc}")

    print(
        f"[{unidade}] PAGINACAO_HTTP_FINAL paginas={completed}/{LISTING_PAGES} "
        f"contratos={len(found)}"
    )
    return list(found.values())[:MAX_CONTRACTS]


# Substitui somente a descoberta de contratos; toda a coleta de detalhes,
# criticidade, faltas, disciplinas, reposicoes e persistencia permanece em scraper.py.
scraper.discover_contracts = optimized_discover_contracts


if __name__ == "__main__":
    scraper.main()
