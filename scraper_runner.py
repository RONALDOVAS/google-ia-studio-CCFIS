"""Runner seguro do scraping CGD.

A listagem usa UMA sessão autenticada. Não cria processos/browsers por página e
não inventa parâmetros de URL. Quando o botão Próxima fornece href, usamos a
URL real; caso contrário, usamos o mecanismo de paginação já validado no
scraper.py. A paralelização permanece somente na etapa de detalhes.
"""
import os
import re
from urllib.parse import urlparse
import scraper

LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", os.getenv("CGD_MAX_LINK_PAGES", "829"))))
LISTING_WAIT_MS = max(0, int(os.getenv("CGD_LISTING_WAIT_MS", "100")))
LISTING_PATHS = {
    "/alunos",
    "/relatorios/alunos",
    "/relatorios/individuais/alunos-curso",
}
NEXT_SELECTORS = [
    'a[rel="next"]', 'button[rel="next"]',
    'a[aria-label*="next" i]', 'button[aria-label*="next" i]',
    'a[aria-label*="proxima" i]', 'button[aria-label*="proxima" i]',
    'a:has-text("Próxima")', 'button:has-text("Próxima")',
    'a:has-text("Proxima")', 'button:has-text("Proxima")',
    'a:has-text("Next")', 'button:has-text("Next")',
    'a:has-text("›")', 'button:has-text("›")',
]


def _listing_source(page, destino):
    if destino and scraper.same_host(destino):
        path = urlparse(destino).path.rstrip("/").lower()
        if path in LISTING_PATHS:
            return destino
    if not scraper.open_page(page, scraper.CGD_URL, "", "inicio", LISTING_WAIT_MS):
        return None
    for _, href in scraper.links(page):
        if urlparse(href).path.rstrip("/").lower() in LISTING_PATHS:
            return href
    return None


def _next_href(page):
    """Retorna somente o href REAL do controle Próxima, se existir."""
    for sel in NEXT_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                e = loc.nth(i)
                if not e.is_visible():
                    continue
                if (e.get_attribute("aria-disabled") or "").lower() == "true":
                    continue
                if "disabled" in (e.get_attribute("class") or "").lower():
                    continue
                href = e.get_attribute("href")
                if href:
                    href = scraper.abs_url(page, href)
                    if scraper.same_host(href):
                        return href
        except Exception:
            pass
    return None


def _next_click(page):
    """Fallback seguro: um único clique na Próxima, na mesma sessão."""
    before = scraper.body(page)[:12000]
    for sel in NEXT_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                e = loc.nth(i)
                if not e.is_visible():
                    continue
                if (e.get_attribute("aria-disabled") or "").lower() == "true":
                    continue
                if "disabled" in (e.get_attribute("class") or "").lower():
                    continue
                e.click(timeout=5000)
                page.wait_for_timeout(LISTING_WAIT_MS)
                if scraper.body(page)[:12000] != before:
                    return True
        except Exception:
            pass
    return False


def optimized_discover_contracts(page, unidade, destino):
    found = {}
    source = _listing_source(page, destino)
    if not source:
        print(f"[{unidade}] LISTAGEM_NAO_ENCONTRADA")
        return []

    if not scraper.open_page(page, source, unidade, "lista_alunos", LISTING_WAIT_MS):
        return []

    print(f"[{unidade}] PAGINACAO SEGURA: uma sessao, limite={LISTING_PAGES}")
    seen_urls = set()
    seen_signatures = set()

    for n in range(1, LISTING_PAGES + 1):
        current = page.url
        signature = scraper.body(page)[:12000]
        if current in seen_urls and signature in seen_signatures:
            print(f"[{unidade}] PAGINACAO_CICLO_DETECTADO pagina={n}")
            break
        seen_urls.add(current)
        seen_signatures.add(signature)

        scraper.collect_contracts(page, found)
        if n == 1 or n % 25 == 0 or n == LISTING_PAGES:
            print(f"[{unidade}] pagina_lista={n} contratos_acumulados={len(found)} url={current}")

        if len(found) >= scraper.MAX_CONTRACTS:
            break

        # Primeiro tentamos a URL que o próprio CGD expõe no controle.
        href = _next_href(page)
        if href:
            if not scraper.open_page(page, href, unidade, f"lista_pagina_{n+1}", LISTING_WAIT_MS):
                break
            continue

        # Se o CGD não expõe href, não inventamos ?page=. Usamos o clique real.
        if not _next_click(page):
            print(f"[{unidade}] FIM_PAGINACAO pagina={n}")
            break

    contracts = list(found.values())[:scraper.MAX_CONTRACTS]
    print(f"[{unidade}] CONTRATOS UNICOS DESCOBERTOS: {len(contracts)}")
    print(f"[{unidade}] DETAIL_WORKERS: {scraper.DETAIL_WORKERS}")
    return contracts


# Substitui apenas a descoberta. A coleta detalhada e o Supabase continuam no
# scraper.py, inclusive os workers de detalhes já existentes.
scraper.discover_contracts = optimized_discover_contracts

if __name__ == "__main__":
    scraper.main()
