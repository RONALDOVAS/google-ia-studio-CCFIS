"""Executor do scraper CGD com descoberta otimizada.

A coleta de detalhes permanece centralizada em scraper.py. Este executor
substitui apenas a descoberta serial e redundante de contratos por uma
paginação orientada a URLs, sem qualquer filtro por cor.
"""

import re
from urllib.parse import urlparse

import scraper


LISTING_PATHS = {
    "/alunos",
    "/relatorios/alunos",
    "/relatorios/individuais/alunos-curso",
}

NEXT_SELECTORS = [
    'a[rel="next"]',
    'button[rel="next"]',
    'a[aria-label*="next" i]',
    'button[aria-label*="next" i]',
    'a[aria-label*="proxima" i]',
    'button[aria-label*="proxima" i]',
    'a:has-text("Próxima")',
    'button:has-text("Próxima")',
    'a:has-text("Proxima")',
    'button:has-text("Proxima")',
    'a:has-text("Next")',
    'button:has-text("Next")',
    'a:has-text("›")',
    'button:has-text("›")',
]


def _is_listing(url):
    try:
        return urlparse(url).path.rstrip("/").lower() in LISTING_PATHS
    except Exception:
        return False


def _next_page_fast(page, unidade):
    """Avança sem varrer o body inteiro; usa href quando a paginação fornece URL."""
    current = page.url
    for selector in NEXT_SELECTORS:
        try:
            loc = page.locator(selector)
            for i in range(loc.count()):
                element = loc.nth(i)
                if not element.is_visible():
                    continue
                if (element.get_attribute("aria-disabled") or "").lower() == "true":
                    continue
                if "disabled" in (element.get_attribute("class") or "").lower():
                    continue

                href = element.get_attribute("href")
                if href and not href.startswith("#"):
                    target = scraper.abs_url(page, href)
                    if target and target != current and scraper.same_host(target):
                        if scraper.open_page(page, target, unidade, "pagina_lista"):
                            return True
                        continue

                before = set()
                try:
                    for _, h in scraper.links(page):
                        cid = scraper.contract_id(h)
                        if cid:
                            before.add(cid)
                except Exception:
                    pass

                element.click()
                for _ in range(20):
                    page.wait_for_timeout(100)
                    if page.url != current:
                        return True
                    try:
                        after = set()
                        for _, h in scraper.links(page):
                            cid = scraper.contract_id(h)
                            if cid:
                                after.add(cid)
                        if after and after != before:
                            return True
                    except Exception:
                        pass
        except Exception:
            pass
    return False


def _paginate_listing(page, unidade, found):
    """Percorre somente a listagem necessária e encerra por URL repetida."""
    seen_urls = set()
    last_count = -1

    for page_number in range(1, scraper.MAX_PAGES + 1):
        current = page.url
        if current in seen_urls:
            break
        seen_urls.add(current)

        before = len(found)
        scraper.collect_contracts(page, found)
        added = len(found) - before
        print(
            f"[{unidade}] pagina_lista={page_number} "
            f"contratos_novos={added} contratos_acumulados={len(found)}"
        )

        if len(found) >= scraper.MAX_CONTRACTS:
            break
        if len(found) == last_count and page_number > 1:
            # Duas páginas sem qualquer contrato novo indicam paginação vazia/repetida.
            break
        last_count = len(found)

        if not _next_page_fast(page, unidade):
            break


def optimized_discover_contracts(page, unidade, destino):
    found = {}

    # Só trata destino como listagem se ele for realmente uma rota de listagem.
    if destino and scraper.same_host(destino) and _is_listing(destino):
        if scraper.open_page(page, destino, unidade, "rota_configurada"):
            _paginate_listing(page, unidade, found)
            if found:
                contracts = list(found.values())[: scraper.MAX_CONTRACTS]
                print(f"[{unidade}] CONTRATOS UNICOS DESCOBERTOS: {len(contracts)}")
                print(f"[{unidade}] DETAIL_WORKERS: {scraper.DETAIL_WORKERS}")
                return contracts

    # Descobre as rotas de listagem uma única vez e usa somente a primeira
    # que efetivamente contém contratos. Não percorre três fontes redundantes.
    if not scraper.open_page(page, scraper.CGD_URL, unidade, "inicio"):
        return []

    sources = []
    for _, href in scraper.links(page):
        path = urlparse(href).path.rstrip("/").lower()
        if path in LISTING_PATHS:
            sources.append(href)

    for source in dict.fromkeys(sources):
        if found:
            break
        if not scraper.open_page(page, source, unidade, "lista_alunos"):
            continue
        _paginate_listing(page, unidade, found)
        if found:
            break

    contracts = list(found.values())[: scraper.MAX_CONTRACTS]
    print(f"[{unidade}] CONTRATOS UNICOS DESCOBERTOS: {len(contracts)}")
    print(f"[{unidade}] DETAIL_WORKERS: {scraper.DETAIL_WORKERS}")
    return contracts


# scraper.main() usa a função pelo nome global durante a execução; substituir
# aqui mantém toda a autenticação, detalhamento paralelo e persistência existentes.
scraper.discover_contracts = optimized_discover_contracts


if __name__ == "__main__":
    scraper.main()
