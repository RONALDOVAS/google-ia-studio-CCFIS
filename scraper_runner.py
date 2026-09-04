"""Executor CGD: descoberta paralela de paginas + detalhamento paralelo.

A descoberta NAO percorre 829 paginas em serie. Ela usa a primeira pagina
apenas para descobrir como o CGD representa a pagina seguinte; depois gera
as URLs das paginas e distribui o trabalho entre workers.
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
    """Retorna qualquer parametro numerico plausivelmente usado para pagina."""
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
                before = page.url
                e.click()
                page.wait_for_timeout(scraper.PAGE_WAIT_MS)
                if page.url != before or scraper.body(page)[:8000] != "":
                    return True
        except Exception:
            pass
    return False


def _pagination_urls(page, base_url):
    """Primeiro tenta hrefs numerados; depois aprende o formato clicando UMA vez."""
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
                candidates.append((key, number, target))
    except Exception:
        pass

    if candidates:
        key = candidates[0][0]
        nums = [n for k, n, _ in candidates if k == key]
        return key, max(nums) if nums else 0

    # Muitos portais deixam o "Próxima" apenas como botão JS. Nesse caso,
    # fazemos UMA única navegação para descobrir o URL real da página 2.
    before_url = page.url
    if _next_click(page):
        after_url = page.url
        key, number = _page_param_from_url(after_url)
        if key:
            return key, number
        # Se o clique alterou o path em vez da query, ainda permitimos o
        # diagnóstico, mas não entramos silenciosamente em paginação serial.
        if after_url != before_url:
            print(f"[{before_url}] NEXT mudou URL para {after_url}, mas o parametro nao foi identificado")

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
        return unidade, url, list(found.values()), None
    except Exception as exc:
        return unidade, url, [], str(exc)


def _parallel_listing(page, unidade, base_url, user, password):
    # Captura a primeira pagina antes de descobrir o next.
    found = {}
    scraper.collect_contracts(page, found)

    key, discovered_last = _pagination_urls(page, base_url)

    if not key:
        raise RuntimeError(
            f"[{unidade}] PAGINACAO NAO IDENTIFICADA: execucao abortada para impedir fallback sequencial. "
            "O scraper nao vai percorrer centenas de paginas uma a uma."
        )

    # Se so conseguimos descobrir a pagina 2, usamos o limite configurado como
    # teto. O workflow atual informa 829, que foi o total observado no CGD.
    last = discovered_last if discovered_last > 1 else LISTING_PAGES
    if last < LISTING_PAGES and discovered_last <= 2:
        last = LISTING_PAGES
    last = min(last, max(1, scraper.MAX_PAGES))

    urls = [_set_page(base_url, key, n) for n in range(1, last + 1)]
    print(
        f"[{unidade}] PAGINACAO PARALELA REAL: parametro={key} paginas={len(urls)} "
        f"workers={LISTING_WORKERS}"
    )

    # A pagina original pode ter sido levada para a pagina 2 pelo diagnostico.
    # Os workers sempre recebem URLs explicitas, inclusive a pagina 1.
    args = [(unidade, url, user, password) for url in urls]
    with ProcessPoolExecutor(max_workers=LISTING_WORKERS) as pool:
        futures = [pool.submit(_listing_worker, a) for a in args]
        completed = 0
        errors = 0
        for future in as_completed(futures):
            completed += 1
            unit, url, contracts, error = future.result()
            for contract in contracts:
                cid = scraper.contract_id(contract)
                if cid:
                    found[cid] = scraper.contract_url(cid)
            if error:
                errors += 1
                print(f"[{unit}] PAGINA_WORKER_ERRO url={url} erro={error}")
            if completed % 10 == 0 or completed == len(futures):
                print(
                    f"[{unidade}] PAGINAS_PROCESSADAS={completed}/{len(futures)} "
                    f"CONTRATOS={len(found)} ERROS={errors}"
                )

    print(f"[{unidade}] PAGINACAO FINALIZADA: paginas={last} contratos={len(found)} erros={errors}")
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
