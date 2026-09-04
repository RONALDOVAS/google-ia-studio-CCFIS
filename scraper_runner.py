"""Executor CGD: paginas da listagem em workers persistentes + detalhes em paralelo.

IMPORTANTE: a listagem NAO cria um processo/browser/login por pagina.
Ela cria no maximo LISTING_WORKERS processos persistentes; cada processo faz
login UMA vez e percorre um bloco de paginas. Assim 829 paginas nao viram
829 logins nem 829 browsers.
"""

import os
import re
from concurrent.futures import ProcessPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse

import scraper

LISTING_PATHS = {
    "/alunos",
    "/relatorios/alunos",
    "/relatorios/individuais/alunos-curso",
}

LISTING_WORKERS = max(1, int(os.getenv("CGD_LISTING_WORKERS", "12")))
LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "829")))

NEXT_SELECTORS = [
    'a[rel="next"]', 'button[rel="next"]',
    'a[aria-label*="next" i]', 'button[aria-label*="next" i]',
    'a[aria-label*="proxima" i]', 'button[aria-label*="proxima" i]',
    'a:has-text("Próxima")', 'button:has-text("Próxima")',
    'a:has-text("Proxima")', 'button:has-text("Proxima")',
    'a:has-text("Next")', 'button:has-text("Next")',
    'a:has-text("›")', 'button:has-text("›")',
]


def _is_listing(url):
    return urlparse(url).path.rstrip("/").lower() in LISTING_PATHS


def _page_param_from_url(url):
    q = parse_qs(urlparse(url).query, keep_blank_values=True)
    preferred = {"page", "pagina", "p", "page_number", "pag", "pageindex", "page_index"}
    for key, values in q.items():
        if values and re.fullmatch(r"\d+", values[-1]) and int(values[-1]) >= 1 and key.lower() in preferred:
            return key, int(values[-1])
    for key, values in q.items():
        if values and re.fullmatch(r"\d+", values[-1]) and int(values[-1]) >= 1:
            return key, int(values[-1])
    return None, None


def _set_page(url, key, number):
    parts = urlparse(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    q[key] = [str(number)]
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


def _next_click(page):
    before_url = page.url
    before_body = scraper.body(page)[:12000]
    for selector in NEXT_SELECTORS:
        try:
            loc = page.locator(selector)
            for i in range(loc.count()):
                e = loc.nth(i)
                if not e.is_visible():
                    continue
                if (e.get_attribute("aria-disabled") or "").lower() == "true":
                    continue
                if "disabled" in (e.get_attribute("class") or "").lower():
                    continue
                e.click()
                page.wait_for_timeout(scraper.PAGE_WAIT_MS)
                if page.url != before_url or scraper.body(page)[:12000] != before_body:
                    return True
        except Exception:
            pass
    return False


def _pagination_pattern(page, base_url):
    """Descobre o parametro de pagina sem iniciar qualquer varredura serial."""
    candidates = []
    try:
        anchors = page.locator("a[href]")
        for i in range(min(anchors.count(), 3000)):
            e = anchors.nth(i)
            href = e.get_attribute("href")
            if not href:
                continue
            target = scraper.abs_url(page, href)
            if not target or not scraper.same_host(target):
                continue
            key, number = _page_param_from_url(target)
            if key:
                candidates.append((key, number))
    except Exception:
        pass

    if candidates:
        key = candidates[0][0]
        nums = [n for k, n in candidates if k == key]
        return key, max(nums) if nums else None

    before = page.url
    if _next_click(page):
        after = page.url
        key, number = _page_param_from_url(after)
        if key:
            return key, number
        if after != before:
            print(f"[PAGINACAO] NEXT mudou URL para {after}, parametro numerico nao identificado")

    return None, None


def _listing_chunk_worker(args):
    """Um worker persistente: UMA sessao Edge/login para varias paginas."""
    unidade, base_url, key, pages, user, password, worker_no = args
    found = {}
    browser = None
    try:
        with scraper.sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context()
            page = context.new_page()

            scraper.login(page, user, password, unidade)
            print(
                f"[{unidade}] LISTING_WORKER={worker_no} LOGIN_OK "
                f"paginas={pages[0]}-{pages[-1]} total={len(pages)}"
            )

            for pos, page_number in enumerate(pages, 1):
                url = _set_page(base_url, key, page_number)
                if scraper.open_page(page, url, unidade, f"lista_pagina_{page_number}", 50):
                    scraper.collect_contracts(page, found)
                else:
                    print(
                        f"[{unidade}] LISTING_WORKER={worker_no} "
                        f"PAGINA_ERRO={page_number}"
                    )

                if pos == 1 or pos % 10 == 0 or pos == len(pages):
                    print(
                        f"[{unidade}] LISTING_WORKER={worker_no} "
                        f"PROGRESSO={pos}/{len(pages)} CONTRATOS={len(found)}"
                    )

            return unidade, worker_no, list(found.values()), None
    except Exception as exc:
        return unidade, worker_no, [], str(exc)
    finally:
        try:
            if browser:
                browser.close()
        except Exception:
            pass


def _split_pages(last, workers):
    workers = min(max(1, workers), last)
    chunks = [[] for _ in range(workers)]
    for index, number in enumerate(range(1, last + 1)):
        chunks[index % workers].append(number)
    return [c for c in chunks if c]


def _parallel_listing(page, unidade, base_url, user, password):
    found = {}
    scraper.collect_contracts(page, found)

    key, discovered_last = _pagination_pattern(page, base_url)
    if not key:
        raise RuntimeError(
            f"[{unidade}] PAGINACAO NAO IDENTIFICADA: execucao abortada. "
            "Nao existe fallback sequencial."
        )

    # O CGD tem 829 paginas observadas. Links visiveis normalmente mostram
    # apenas uma janela (ex.: 1..10), portanto nao usamos essa janela como
    # total. O valor configurado pelo workflow e o teto autoritativo.
    configured = LISTING_PAGES
    last = configured
    if discovered_last and discovered_last > configured:
        last = discovered_last
    last = min(last, max(1, scraper.MAX_PAGES))

    chunks = _split_pages(last, LISTING_WORKERS)
    print(
        f"[{unidade}] PAGINACAO PARALELA REAL: parametro={key} paginas={last} "
        f"workers={len(chunks)}"
    )
    print(
        f"[{unidade}] MODO=WORKERS_PERSISTENTES: {len(chunks)} browsers/logins "
        f"para {last} paginas; NAO 1 browser/login por pagina"
    )

    args = []
    for worker_no, chunk in enumerate(chunks, 1):
        args.append((unidade, base_url, key, chunk, user, password, worker_no))

    errors = 0
    with ProcessPoolExecutor(max_workers=len(chunks)) as pool:
        futures = [pool.submit(_listing_chunk_worker, a) for a in args]
        for future in as_completed(futures):
            unit, worker_no, contracts, error = future.result()
            if error:
                errors += 1
                print(
                    f"[{unit}] LISTING_WORKER_ERRO worker={worker_no} erro={error}"
                )
                continue
            for contract in contracts:
                cid = scraper.contract_id(contract)
                if cid:
                    found[cid] = scraper.contract_url(cid)
            print(
                f"[{unit}] LISTING_WORKER_FINALIZADO worker={worker_no} "
                f"CONTRATOS_ACUMULADOS={len(found)}"
            )

    print(
        f"[{unidade}] PAGINACAO FINALIZADA: paginas={last} "
        f"workers={len(chunks)} contratos={len(found)} erros={errors}"
    )
    return found


def optimized_discover_contracts(page, unidade, destino):
    scraper.PAGE_WAIT_MS = min(scraper.PAGE_WAIT_MS, 150)
    user = scraper.CONFIG[unidade]["usuario"]
    password = scraper.CONFIG[unidade]["senha"]

    if destino and scraper.same_host(destino) and _is_listing(destino):
        if scraper.open_page(page, destino, unidade, "rota_configurada", 150):
            found = _parallel_listing(page, unidade, destino, user, password)
            contracts = list(found.values())[:scraper.MAX_CONTRACTS]
            print(f"[{unidade}] CONTRATOS UNICOS DESCOBERTOS: {len(contracts)}")
            return contracts

    if not scraper.open_page(page, scraper.CGD_URL, unidade, "inicio", 150):
        return []

    sources = []
    for _, href in scraper.links(page):
        if urlparse(href).path.rstrip("/").lower() in LISTING_PATHS:
            sources.append(href)

    for source in dict.fromkeys(sources):
        if not scraper.open_page(page, source, unidade, "lista_alunos", 150):
            continue
        found = _parallel_listing(page, unidade, source, user, password)
        if found:
            contracts = list(found.values())[:scraper.MAX_CONTRACTS]
            print(f"[{unidade}] CONTRATOS UNICOS DESCOBERTOS: {len(contracts)}")
            return contracts

    print(f"[{unidade}] CONTRATOS UNICOS DESCOBERTOS: 0")
    return []


scraper.discover_contracts = optimized_discover_contracts

if __name__ == "__main__":
    scraper.main()
