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
            m = re.search(r"/contratos/(\d+)(?:/)?(?:[?#].*)?$", unescape(href), re.I)
            if m:
                self.ids.add(m.group(1))
            score = 0
            text = " ".join([
                a.get("aria-label", ""), a.get("title", ""),
                a.get("rel", ""), a.get("class", "")
            ]).lower()
            if "next" in text or "proxima" in text or "próxima" in text or "pagination-next" in text:
                score = 10
            if score > self._next_score:
                self._next_score = score
                self.next_href = href
                self._in_next = True

    def handle_data(self, data):
        if self._in_next and self._next_score >= 0:
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
    contracts = {
        cid: scraper.contract_url(cid)
        for cid in p.ids
    }
    href = None
    if p.next_href:
        href = urljoin(base_url, unescape(p.next_href)).split("#", 1)[0]
        if not scraper.same_host(href):
            href = None
    return contracts, href


def _listing_source(page, destino, unidade):
    if destino and scraper.same_host(destino):
        path = urlparse(destino).path.rstrip("/").lower()
        if path in ("/alunos", "/relatorios/alunos", "/relatorios/individuais/alunos-curso"):
            return destino
    # Evita links(page), que varre milhares de elementos. Procura somente
    # hrefs candidatos a listagem.
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
        return None, None, None

    captured = []
    def on_request(req):
        try:
            if scraper.same_host(req.url) and req.resource_type in ("document", "xhr", "fetch"):
                captured.append(req)
        except Exception:
            pass

    page.on("request", on_request)
    before = page.url
    try:
        e.click(timeout=7000)
        page.wait_for_timeout(max(500, LISTING_WAIT_MS + 400))
    except Exception as exc:
        page.remove_listener("request", on_request)
        print(f"[PAGINACAO] clique inicial falhou: {exc}")
        return None, None, None
    finally:
        try:
            page.remove_listener("request", on_request)
        except Exception:
            pass

    req = captured[-1] if captured else None
    response_url = page.url if page.url != before else None
    return req, response_url, page


def _request_kwargs(req):
    """Extrai apenas dados reproduziveis, sem copiar headers problematicos."""
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
    r = api.get(url, headers=headers or {}, timeout=scraper.PAGE_TIMEOUT_MS)
    return r


def _api_post(api, url, post_data, headers=None):
    headers = dict(headers or {})
    ctype = (headers.get("content-type") or "").lower()
    if ctype.startswith("application/json"):
        return api.post(url, data=post_data or "", headers=headers, timeout=scraper.PAGE_TIMEOUT_MS)
    return api.post(url, form=dict(parse_qsl(post_data or "", keep_blank_values=True)), headers=headers, timeout=scraper.PAGE_TIMEOUT_MS)


def _replayable_get(method, url, post_data, headers):
    return method == "GET" and bool(url) and not post_data


def optimized_discover_contracts(page, unidade, destino):
    found = {}
    source = _listing_source(page, destino, unidade)
    if not source:
        raise RuntimeError(f"[{unidade}] LISTAGEM_NAO_ENCONTRADA")
    if not scraper.open_page(page, source, unidade, "lista_pagina_1", LISTING_WAIT_MS):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM")

    # A pagina 1 vem do browser autenticado e prova que a listagem esta acessivel.
    html = page.content()
    page_contracts, next_href = _parse_html(html, page.url)
    found.update(page_contracts)
    print(f"[{unidade}] PAGINA_HTTP=1/{LISTING_PAGES} contratos={len(found)}")

    # Captura a requisicao real do botao. Nao criamos ?page=, ?pagina= etc.
    req, browser_next_url, _ = _capture_next(page)
    if req is None:
        if next_href:
            # Caso o controle seja um link normal, sua URL ja e a requisicao real.
            try:
                with page.context.request as _:
                    pass
            except Exception:
                pass
            method, captured_url, post_data, headers = "GET", next_href, None, {"referer": source}
        else:
            print(f"[{unidade}] LISTAGEM_SO_UMA_PAGINA_OU_NEXT_NAO_CAPTURADO")
            return list(found.values())[:MAX_CONTRACTS]
    else:
        method, captured_url, post_data, headers = _request_kwargs(req)

    print(f"[{unidade}] PAGINACAO_REAL method={method} url={captured_url}")

    # O contexto API compartilha os cookies da sessao autenticada do Edge.
    api = page.context.request
    current_url = browser_next_url or captured_url
    seen = {source}

    # GET e o caso ideal: depois da primeira requisicao real, seguimos os hrefs
    # reais que o proprio CGD devolve, sem abrir novas abas/janelas.
    if method == "GET":
        if current_url and current_url not in seen:
            try:
                r = _api_get(api, current_url, headers=headers)
                if r.ok:
                    body = r.text()
                    cs, nh = _parse_html(body, current_url)
                    found.update(cs)
                    seen.add(current_url)
                    print(f"[{unidade}] PAGINA_HTTP=2/{LISTING_PAGES} contratos={len(found)}")
                    next_href = nh
                else:
                    print(f"[{unidade}] HTTP_PAGINA_2_STATUS={r.status}")
                    next_href = None
            except Exception as exc:
                print(f"[{unidade}] HTTP_PAGINA_2_ERRO={exc}")
                next_href = None
        else:
            next_href = None

        page_no = 2
        while next_href and page_no < LISTING_PAGES and len(found) < MAX_CONTRACTS:
            if next_href in seen:
                print(f"[{unidade}] CICLO_DE_PAGINACAO url={next_href}")
                break
            try:
                r = _api_get(api, next_href, headers={"referer": current_url or source})
                if not r.ok:
                    print(f"[{unidade}] HTTP_STATUS pagina={page_no+1} status={r.status}")
                    break
                body = r.text()
                cs, nh = _parse_html(body, next_href)
                before = len(found)
                found.update(cs)
                seen.add(next_href)
                current_url = next_href
                next_href = nh
                page_no += 1
                if page_no == 3 or page_no % 25 == 0 or page_no == LISTING_PAGES:
                    print(f"[{unidade}] PAGINA_HTTP={page_no}/{LISTING_PAGES} contratos={len(found)} novos={len(found)-before}")
                if not cs and not nh:
                    break
            except Exception as exc:
                print(f"[{unidade}] HTTP_ERRO pagina={page_no+1}: {exc}")
                break

        print(f"[{unidade}] PAGINACAO_HTTP_FINAL paginas_processadas={page_no} contratos={len(found)}")
        return list(found.values())[:MAX_CONTRACTS]

    # POST/AJAX: ainda sem browser por pagina. Reproduzimos a requisicao capturada.
    # Se o CGD exigir estado dinâmico que não possa ser reproduzido com o payload
    # capturado, falhamos explicitamente em vez de voltar ao scraper lento.
    if not post_data:
        raise RuntimeError(f"[{unidade}] PAGINACAO_POST_SEM_PAYLOAD_REPRODUZIVEL")

    r = None
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
    if not cs and not nh:
        print(f"[{unidade}] POST_RETORNO_SEM_CONTRATOS_E_SEM_NEXT")
    else:
        print(f"[{unidade}] POST_REPLAY_CONFIRMADO contratos={len(found)} next={bool(nh)}")
    # Nao inventamos como incrementar um POST stateful. Para evitar outra
    # execucao travada, encerramos com os dados que foram comprovadamente lidos.
    return list(found.values())[:MAX_CONTRACTS]


scraper.discover_contracts = optimized_discover_contracts

if __name__ == "__main__":
    scraper.main()
