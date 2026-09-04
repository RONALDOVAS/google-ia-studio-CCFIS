"""Runner CGD: uma sessao Playwright para login/descoberta e paginacao por HTTP.

A primeira pagina e o clique real em "Proxima" sao usados apenas para descobrir
como o CGD pagina. Depois disso as paginas sao lidas pela APIRequestContext do
mesmo BrowserContext autenticado, sem abrir um Edge por pagina e sem inventar
parametros de URL.
"""
import os
import re
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlparse, urljoin, parse_qsl
import scraper

LISTING_PAGES = max(1, int(os.getenv("CGD_LISTING_PAGES", "829")))
LISTING_WAIT_MS = max(0, int(os.getenv("CGD_LISTING_WAIT_MS", "100")))
MAX_CONTRACTS = scraper.MAX_CONTRACTS

NEXT_SELECTORS = [
    'a[rel="next"]', 'button[rel="next"]',
    'a[aria-label*="next" i]', 'button[aria-label*="next" i]',
    'a[aria-label*="proxima" i]', 'button[aria-label*="proxima" i]',
    'a:has-text("Próxima")', 'button:has-text("Próxima")',
    'a:has-text("Proxima")', 'button:has-text("Proxima")',
    'a:has-text("Next")', 'button:has-text("Next")',
    'a:has-text("›")', 'button:has-text("›")',
]


class _ContractParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = set()
        self.next_href = None
        self._next_score = -1
        self._in_next = False

    def handle_starttag(self, tag, attrs):
        a = {str(k).lower(): (v or "") for k, v in attrs}
        href = a.get("href", "")
        if href:
            decoded = unescape(href)
            m = re.search(r"/contratos/(\d+)(?:/)?(?:[?#].*)?$", decoded, re.I)
            if m:
                self.ids.add(m.group(1))
            text = " ".join([
                a.get("aria-label", ""), a.get("title", ""),
                a.get("rel", ""), a.get("class", "")
            ]).lower()
            score = 10 if any(x in text for x in ("next", "proxima", "próxima", "pagination-next")) else 0
            if score > self._next_score:
                self._next_score = score
                self.next_href = href
                self._in_next = True

    def handle_data(self, data):
        if self._in_next:
            t = data.strip().lower()
            if "próxima" in t or "proxima" in t or t == "next" or t == "›":
                self._next_score = max(self._next_score, 20)

    def handle_endtag(self, tag):
        if tag.lower() in ("a", "button"):
            self._in_next = False


def _parse_html(html, base_url):
    p = _ContractParser()
    try:
        p.feed(html or "")
    except Exception:
        pass
    contracts = {cid: scraper.contract_url(cid) for cid in p.ids}
    href = None
    if p.next_href:
        href = urljoin(base_url, unescape(p.next_href)).split("#", 1)[0]
        if not scraper.same_host(href):
            href = None
    return contracts, href


def _listing_source(page, destino):
    if destino and scraper.same_host(destino):
        path = urlparse(destino).path.rstrip("/").lower()
        if path in ("/alunos", "/relatorios/alunos", "/relatorios/individuais/alunos-curso"):
            return destino
    try:
        loc = page.locator('a[href*="/alunos"],a[href*="/relatorios/alunos"]')
        for i in range(min(loc.count(), 50)):
            href = loc.nth(i).get_attribute("href")
            if href:
                href = scraper.abs_url(page, href)
                if urlparse(href).path.rstrip("/").lower() in (
                    "/alunos", "/relatorios/alunos", "/relatorios/individuais/alunos-curso"
                ):
                    return href
    except Exception:
        pass
    return None


def _next_element(page):
    for sel in NEXT_SELECTORS:
        try:
            loc = page.locator(sel)
            for i in range(min(loc.count(), 20)):
                e = loc.nth(i)
                if not e.is_visible():
                    continue
                if (e.get_attribute("aria-disabled") or "").lower() == "true":
                    continue
                if "disabled" in (e.get_attribute("class") or "").lower():
                    continue
                return e
        except Exception:
            pass
    return None


def _capture_next(page):
    """Clica UMA vez e captura a requisicao que o CGD realmente disparou."""
    e = _next_element(page)
    if not e:
        return None
    captured = []

    def on_request(req):
        try:
            if scraper.same_host(req.url) and req.resource_type in ("document", "xhr", "fetch"):
                captured.append(req)
        except Exception:
            pass

    page.on("request", on_request)
    try:
        e.click(timeout=7000)
        page.wait_for_timeout(max(500, LISTING_WAIT_MS + 400))
    except Exception as exc:
        print(f"[PAGINACAO] clique inicial falhou: {exc}")
        return None
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass

    req = captured[-1] if captured else None
    return req


def _request_kwargs(req):
    method = (req.method or "GET").upper()
    headers = {}
    try:
        raw = req.all_headers()
        for key in ("accept", "content-type", "x-requested-with", "referer", "origin"):
            if key in raw:
                headers[key] = raw[key]
    except Exception:
        pass
    return method, req.url, req.post_data, headers


def _api_get(api, url, headers=None):
    return api.get(url, headers=headers or {}, timeout=scraper.PAGE_TIMEOUT_MS)


def _api_post(api, url, post_data, headers=None):
    headers = dict(headers or {})
    ctype = (headers.get("content-type") or "").lower()
    if ctype.startswith("application/json"):
        return api.post(url, data=post_data or "", headers=headers, timeout=scraper.PAGE_TIMEOUT_MS)
    return api.post(
        url,
        form=dict(parse_qsl(post_data or "", keep_blank_values=True)),
        headers=headers,
        timeout=scraper.PAGE_TIMEOUT_MS,
    )


def optimized_discover_contracts(page, unidade, destino):
    found = {}
    source = _listing_source(page, destino)
    if not source:
        raise RuntimeError(f"[{unidade}] LISTAGEM_NAO_ENCONTRADA")
    if not scraper.open_page(page, source, unidade, "lista_pagina_1", LISTING_WAIT_MS):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM")

    html = page.content()
    page_contracts, next_href = _parse_html(html, page.url)
    found.update(page_contracts)
    print(f"[{unidade}] PAGINA_HTTP=1/{LISTING_PAGES} contratos={len(found)}")

    req = _capture_next(page)
    if req is None:
        if next_href:
            method, captured_url, post_data, headers = "GET", next_href, None, {"referer": source}
        else:
            print(f"[{unidade}] LISTAGEM_SO_UMA_PAGINA_OU_NEXT_NAO_CAPTURADO")
            return list(found.values())[:MAX_CONTRACTS]
    else:
        method, captured_url, post_data, headers = _request_kwargs(req)

    print(f"[{unidade}] PAGINACAO_REAL method={method} url={captured_url}")
    api = page.context.request

    if method == "GET":
        current_url = captured_url
        page_no = 2
        seen = {source}
        while current_url and page_no <= LISTING_PAGES and len(found) < MAX_CONTRACTS:
            if current_url in seen:
                print(f"[{unidade}] CICLO_DE_PAGINACAO url={current_url}")
                break
            try:
                r = _api_get(api, current_url, headers=headers if page_no == 2 else {"referer": captured_url})
                if not r.ok:
                    print(f"[{unidade}] HTTP_STATUS pagina={page_no} status={r.status}")
                    break
                body = r.text()
                cs, nh = _parse_html(body, current_url)
                before = len(found)
                found.update(cs)
                seen.add(current_url)
                if page_no == 2 or page_no % 25 == 0 or page_no == LISTING_PAGES:
                    print(f"[{unidade}] PAGINA_HTTP={page_no}/{LISTING_PAGES} contratos={len(found)} novos={len(found)-before}")
                if not nh or not cs:
                    break
                captured_url = current_url
                current_url = nh
                page_no += 1
            except Exception as exc:
                print(f"[{unidade}] HTTP_ERRO pagina={page_no}: {exc}")
                break
        print(f"[{unidade}] PAGINACAO_HTTP_FINAL paginas_processadas={min(page_no, LISTING_PAGES)} contratos={len(found)}")
        return list(found.values())[:MAX_CONTRACTS]

    if not post_data:
        raise RuntimeError(f"[{unidade}] PAGINACAO_POST_SEM_PAYLOAD_REPRODUZIVEL")

    try:
        r = _api_post(api, captured_url, post_data, headers=headers)
    except Exception as exc:
        raise RuntimeError(f"[{unidade}] PAGINACAO_POST_ERRO: {exc}")
    if not r.ok:
        raise RuntimeError(f"[{unidade}] PAGINACAO_POST_STATUS={r.status}")
    body = r.text()
    cs, nh = _parse_html(body, captured_url)
    found.update(cs)
    print(f"[{unidade}] PAGINA_HTTP=2/{LISTING_PAGES} contratos={len(found)}")
    print(f"[{unidade}] POST_REPLAY_CONFIRMADO contratos={len(found)} next={bool(nh)}")
    return list(found.values())[:MAX_CONTRACTS]


scraper.discover_contracts = optimized_discover_contracts

if __name__ == "__main__":
    scraper.main()
