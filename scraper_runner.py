"""Executor CGD: descoberta paralela de paginas + detalhamento paralelo.

Nao usa cor de contrato. A listagem e dividida em URLs de pagina e processada
em paralelo. O scraper.py continua responsavel pelo detalhamento e persistencia.
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
]


def _is_listing(url):
    return urlparse(url).path.rstrip("/").lower() in LISTING_PATHS


def _page_param_from_url(url):
    q = parse_qs(urlparse(url).query, keep_blank_values=True)
    for key, values in q.items():
        if not values:
            continue
        value = values[-1]
        if re.fullmatch(r"\d+", value) and int(value) >= 1:
            if key.lower() in {"page", "pagina", "p", "page_number", "pag"}:
                return key, int(value)
    return None, None


def _set_page(url, key, number):
    parts = urlparse(url)
    q = parse_qs(parts.query, keep_blank_values=True)
    q[key] = [str(number)]
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


def _pagination_urls(page, base_url):
    """Descobre o formato real da paginação sem navegar página a página."""
    candidates = []
    try:
        anchors = page.locator("a[href]")
        for i in range(min(anchors.count(), 2000)):
            e = anchors.nth(i)
            href = e.get_attribute("href")
            if not href:
                continue
            target = scraper.abs_url(page, href)
            if not target or not scraper.same_host(target):
                continue
            if urlparse(target).path.rstrip("/").lower() != urlparse(base_url).path.rstrip("/").lower():
                continue
            key, number = _page_param_from_url(target)
            if key:
                candidates.append((key, number, target))
    except Exception:
        pass

    if candidates:
        key = candidates[0][0]
        highest = max(n for k, n, _ in candidates if k == key)
        return key, max(1, highest)

    # Se a página só expõe o link "Próxima", extraímos dele o parâmetro.
    for selector in NEXT_SELECTORS:
        try:
            loc = page.locator(selector)
            for i in range(loc.count()):
                e = loc.nth(i)
                href = e.get_attribute("href")
                if not href:
                    continue
                target = scraper.abs_url(page, href)
                key, number = _page_param_from_url(target)
                if key:
                    return key, 0
        except Exception:
            pass

    return None, None


def _listing_worker(args):
    unidade, url, user, password = args
    found = {}
    try:
        with scraper.sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=True)
            context = browser.new_context()
            page = context.new_page()
            scraper.login(page, user, password, unidade)
            if scraper.open_page(page, url, unidade, "pagina_lista", 100):
                scraper.collect_contracts(page, found)
            browser.close()
        return unidade, list(found.values()), None
    except Exception as exc:
        return unidade, [], str(exc)


def _parallel_listing(page, unidade, base_url, user, password):
    key, explicit_last = _pagination_urls(page, base_url)

    if not key:
        print(f"[{unidade}] PAGINACAO: parametro de pagina nao identificado; usando fallback sequencial")
        found = {}
        seen = set()
        for n in range(1, scraper.MAX_PAGES + 1):
            if page.url in seen:
                break
            seen.add(page.url)
            scraper.collect_contracts(page, found)
            if len(found) >= scraper.MAX_CONTRACTS:
                break
            # Usa a função otimizada apenas como último recurso.
            moved = False
            for selector in NEXT_SELECTORS:
                try:
                    loc = page.locator(selector)
                    if loc.count() and loc.first.is_visible():
                        href = loc.first.get_attribute("href")
                        if href:
                            target = scraper.abs_url(page, href)
                            if scraper.open_page(page, target, unidade, "pagina_lista", 100):
                                moved = True
                                break
                except Exception:
                    pass
            if not moved:
                break
        return found

    # Quando a paginação fornece números, não existe motivo para caminhar 1->2->3.
    # Montamos todas as URLs e distribuímos o trabalho entre workers independentes.
    last = explicit_last if explicit_last >= 1 else LISTING_PAGES
    last = min(last, max(1, scraper.MAX_PAGES))
    urls = [_set_page(base_url, key, n) for n in range(1, last + 1)]
    print(f"[{unidade}] PAGINACAO PARALELA: parametro={key} paginas={len(urls)} workers={LISTING_WORKERS}")

    found = {}
    args = [(unidade, url, user, password) for url in urls]
    with ProcessPoolExecutor(max_workers=LISTING_WORKERS) as pool:
        futures = [pool.submit(_listing_worker, a) for a in args]
        completed = 0
        for future in as_completed(futures):
            completed += 1
            unit, contracts, error = future.result()
            for url in contracts:
                cid = scraper.contract_id(url)
                if cid:
                    found[cid] = scraper.contract_url(cid)
            if error:
                print(f"[{unit}] pagina_worker_erro={error}")
            if completed % 10 == 0 or completed == len(futures):
                print(f"[{unidade}] paginas_processadas={completed}/{len(futures)} contratos={len(found)}")
            if len(found) >= scraper.MAX_CONTRACTS:
                break
    return found


def optimized_discover_contracts(page, unidade, destino):
    scraper.PAGE_WAIT_MS = min(scraper.PAGE_WAIT_MS, 150)
    user = scraper.CONFIG[unidade]["usuario"]
    password = scraper.CONFIG[unidade]["senha"]

    # Preferimos a rota configurada se ela for realmente uma listagem.
    if destino and scraper.same_host(destino) and _is_listing(destino):
        if scraper.open_page(page, destino, unidade, "rota_configurada", 150):
            found = _parallel_listing(page, unidade, destino, user, password)
            if found:
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
