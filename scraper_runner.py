"""Executor CGD otimizado a partir da estrategia de raspagem assíncrona do projeto.

Principios desta versão:
- usa /horarios como primeira rota conhecida do CGD;
- autentica uma única sessão Edge por unidade;
- descobre os URLs reais da paginação na primeira página;
- processa páginas da listagem em paralelo, com limite conservador;
- não revarre /alunos + relatórios em sequência;
- vermelho/inativo é descartado e azul/ativo é reservado;
- contratos ativos só são detalhados depois da listagem;
- detalhes continuam em uma sessão estável e sequencial para não invalidar a sessão;
- imagens, mídia e fontes são bloqueadas durante a listagem;
- --no-sandbox é removido explicitamente.
"""
import asyncio
import json
import os
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, urljoin

import scraper
from playwright.async_api import async_playwright

scraper.MAX_PAGES = int(os.getenv("CGD_MAX_LINK_PAGES", "5000"))
scraper.MAX_CONTRACTS = int(os.getenv("CGD_MAX_CONTRACTS", "10000"))
scraper.dump = lambda page, unidade, nome: None

PAGE_WAIT_MS = int(os.getenv("CGD_PAGE_WAIT_MS", "180"))
LIST_WAIT_MS = int(os.getenv("CGD_LIST_WAIT_MS", "100"))
LIST_CONCURRENCY = max(2, min(6, int(os.getenv("CGD_LIST_CONCURRENCY", "4"))))

BASE = scraper.CGD_URL.rstrip("/")


def is_login_url(url):
    return "/login" in urlparse(str(url)).path.lower()


def contract_id(url):
    m = re.search(r"/contratos/(\d+)$", urlparse(url).path.rstrip("/"), re.I)
    return m.group(1) if m else None


def contract_url(cid):
    return f"{BASE}/contratos/{cid}"


def normalize_url(page_url, href):
    return urljoin(page_url, href or "").split("#", 1)[0]


def classify_item(item):
    """Classifica primeiro a linha; não usa a cor azul padrão do hyperlink."""
    row_text = " ".join(str(item.get(k) or "").lower() for k in (
        "rowClass", "rowStyle", "rowTitle", "rowAria", "rowColor"
    ))
    anchor_text = " ".join(str(item.get(k) or "").lower() for k in (
        "class", "style", "title", "aria"
    ))

    if any(x in row_text for x in (
        "text-danger", "bg-danger", "danger", "vermelho", "red",
        "inativo", "inactive", "cancelado", "encerrado"
    )):
        return "red"
    if any(x in row_text for x in (
        "text-primary", "bg-primary", "text-info", "bg-info", "azul", "blue",
        "ativo", "active", "vigente"
    )):
        return "blue"

    # Só considera a âncora se ela própria trouxer uma classe/status explícita.
    if any(x in anchor_text for x in (
        "text-danger", "bg-danger", "danger", "vermelho", "red",
        "inativo", "inactive", "cancelado", "encerrado"
    )):
        return "red"
    if any(x in anchor_text for x in (
        "text-primary", "bg-primary", "text-info", "bg-info", "azul", "blue",
        "ativo", "active", "vigente"
    )):
        return "blue"

    # Computed style somente da LINHA. O azul default do <a> nunca decide.
    for key in ("rowColor",):
        value = str(item.get(key) or "")
        for m in re.finditer(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", value.lower()):
            r, g, b = map(int, m.groups())
            if r >= 150 and r >= g * 1.45 and r >= b * 1.45 and g <= 150 and b <= 150:
                return "red"
            if b >= 120 and b >= r * 1.20 and b >= g * 1.05:
                return "blue"
    return "unknown"


async def prepare_page(page):
    async def route_handler(route):
        rt = route.request.resource_type
        if rt in ("image", "media", "font"):
            await route.abort()
        else:
            await route.continue_()
    await page.route("**/*", route_handler)


async def login_async(page, user, password, unidade):
    await page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    us = page.locator('input[type="text"],input[type="email"],input[name*="user" i],input[name*="login" i],input[name*="email" i]')
    ps = page.locator('input[type="password"],input[name*="senha" i],input[name*="password" i]')
    U = P = None
    for i in range(await us.count()):
        e = us.nth(i)
        if await e.is_visible():
            U = e
            break
    for i in range(await ps.count()):
        e = ps.nth(i)
        if await e.is_visible():
            P = e
            break
    if not U or not P:
        raise RuntimeError(f"[{unidade}] campos de login não encontrados")
    if not user or not password:
        raise RuntimeError(f"[{unidade}] credenciais ausentes")
    await U.fill(user)
    await P.fill(password)
    bs = page.locator('button[type="submit"],input[type="submit"],button:has-text("Entrar"),button:has-text("Acessar"),button:has-text("Login")')
    B = None
    for i in range(await bs.count()):
        e = bs.nth(i)
        if await e.is_visible():
            B = e
            break
    if not B:
        raise RuntimeError(f"[{unidade}] botão de login não encontrado")
    await B.click()
    await page.wait_for_timeout(3500)
    if is_login_url(page.url):
        raise RuntimeError(f"[{unidade}] login não autenticou: {page.url}")
    print(f"[{unidade}] LOGIN OK: {page.url}")


async def extract_contracts(page, unidade):
    items = await page.locator('a[href*="/contratos/"]').evaluate_all("""
        els => els.map(a => {
            const tr = a.closest('tr');
            const nodes = [tr, tr && tr.parentElement].filter(Boolean);
            const attrs = (n, p) => nodes.map(x => x.getAttribute(p)).filter(Boolean).join(' ');
            const styles = nodes.map(x => {
                const s = getComputedStyle(x);
                return [s.color, s.backgroundColor, s.borderColor].join(' ');
            }).join(' ');
            return {
                href: a.href,
                class: a.getAttribute('class') || '',
                style: a.getAttribute('style') || '',
                title: a.getAttribute('title') || '',
                aria: a.getAttribute('aria-label') || '',
                rowClass: attrs(tr, 'class'),
                rowStyle: attrs(tr, 'style'),
                rowTitle: attrs(tr, 'title'),
                rowAria: attrs(tr, 'aria-label'),
                rowColor: styles
            };
        })
    """)
    found = {}
    red = 0
    unknown = 0
    for item in items or []:
        href = normalize_url(page.url, item.get("href"))
        cid = contract_id(href)
        if not cid or cid in found:
            continue
        color = classify_item(item)
        if color == "red":
            red += 1
        elif color == "blue":
            found[cid] = contract_url(cid)
        else:
            unknown += 1
    if red or unknown:
        print(f"[{unidade}] página {page.url}: azuis={len(found)} vermelhos={red} não_identificados={unknown}")
    return found


async def pagination_urls(page, unidade):
    """Extrai URLs de páginas e tenta determinar o total sem clicar 829 vezes."""
    data = await page.locator("a[href],button").evaluate_all("""
        els => els.map(e => ({
            tag:e.tagName,
            text:(e.innerText||'').trim(),
            href:e.href||e.getAttribute('href')||'',
            aria:e.getAttribute('aria-label')||'',
            rel:e.getAttribute('rel')||'',
            cls:e.getAttribute('class')||''
        })).filter(x => x.href || x.text || x.aria)
    """)
    base = page.url
    numbered = {}
    last_href = None
    for x in data:
        href = normalize_url(base, x.get("href"))
        txt = str(x.get("text") or "").strip()
        aria = str(x.get("aria") or "").lower()
        if not href or is_login_url(href):
            continue
        if re.fullmatch(r"\d{1,5}", txt):
            n = int(txt)
            if 1 <= n <= scraper.MAX_PAGES:
                numbered[n] = href
        if any(k in (txt.lower() + " " + aria) for k in ("última", "ultima", "last")):
            last_href = href

    # Caso clássico: links numerados já revelam todas as páginas.
    if len(numbered) >= 2:
        max_n = max(numbered)
        if last_href and max_n < scraper.MAX_PAGES:
            numbered[max_n] = numbered[max_n]
        print(f"[{unidade}] PAGINAÇÃO EXPLÍCITA: {len(numbered)} páginas visíveis")
        return [numbered[n] for n in sorted(numbered)]

    # Caso a UI mostre apenas a página atual + Última, usa o href da última
    # para inferir o parâmetro de página e gera os URLs diretamente.
    candidates = [x for x in data if x.get("href")]
    refs = [normalize_url(base, x.get("href")) for x in candidates]
    refs = [u for u in refs if urlparse(u).netloc == urlparse(base).netloc]
    if last_href:
        q = parse_qs(urlparse(last_href).query)
        for key, vals in q.items():
            if not vals or not vals[0].isdigit():
                continue
            n = int(vals[0])
            if n > 1 and n <= scraper.MAX_PAGES:
                urls = []
                for page_no in range(1, n + 1):
                    p = urlparse(last_href)
                    qq = parse_qs(p.query)
                    qq[key] = [str(page_no)]
                    urls.append(urlunparse((p.scheme, p.netloc, p.path, p.params, urlencode(qq, doseq=True), p.fragment)))
                print(f"[{unidade}] PAGINAÇÃO INFERIDA: {n} páginas via parâmetro '{key}'")
                return urls

    # Mesmo sem Última, procura um href de próxima página com parâmetro numérico.
    for u in refs:
        q = parse_qs(urlparse(u).query)
        for key, vals in q.items():
            if vals and vals[0].isdigit() and key.lower() in ("page", "pagina", "p", "pageindex", "currentpage"):
                n = int(vals[0])
                if n >= 2:
                    print(f"[{unidade}] PAGINAÇÃO INFERIDA pelo próximo href; parâmetro '{key}'")
                    urls = []
                    for page_no in range(1, min(scraper.MAX_PAGES, n + 1)):
                        p = urlparse(u); qq = parse_qs(p.query); qq[key] = [str(page_no)]
                        urls.append(urlunparse((p.scheme,p.netloc,p.path,p.params,urlencode(qq,doseq=True),p.fragment)))
                    return urls
    return [base]


async def fetch_listing_page(browser, url, storage_state, unidade, sem):
    context = await browser.new_context(storage_state=storage_state, viewport={"width":1440,"height":1000})
    page = await context.new_page()
    await prepare_page(page)
    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        await page.wait_for_timeout(PAGE_WAIT_MS)
        if is_login_url(page.url):
            return {"url": url, "contracts": {}, "error": "session_expirada"}
        contracts = await extract_contracts(page, unidade)
        print(f"[{unidade}] worker {sem}: página processada")
        return {"url": url, "contracts": contracts, "error": None}
    except Exception as exc:
        print(f"[{unidade}] worker {sem}: ERRO {url}: {exc}")
        return {"url": url, "contracts": {}, "error": str(exc)}
    finally:
        await context.close()


async def collect_listing_parallel(unidade, cfg):
    async with async_playwright() as pw:
        context = await pw.chromium.launch_persistent_context(
            user_data_dir=str(scraper.EDGE_PROFILE_BASE / unidade),
            channel="msedge", headless=False,
            viewport={"width":1440,"height":1000},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--no-sandbox"],
        )
        try:
            first = context.pages[0] if context.pages else await context.new_page()
            await prepare_page(first)
            await login_async(first, cfg["usuario"], cfg["senha"], unidade)

            # /horarios é a rota primária fornecida para o CGD. Só usa destino
            # configurado como fallback se /horarios não entregar nenhuma URL.
            sources = [f"{BASE}/horarios"]
            destino = cfg.get("destino")
            if destino and destino not in sources:
                sources.append(destino)

            page_urls = []
            for src in sources:
                await first.goto(src, wait_until="domcontentloaded", timeout=45000)
                await first.wait_for_timeout(LIST_WAIT_MS)
                if is_login_url(first.url):
                    print(f"[{unidade}] ROTA NEGADA: {src} -> {first.url}")
                    continue
                initial = await extract_contracts(first, unidade)
                page_urls = await pagination_urls(first, unidade)
                if initial or len(page_urls) > 1:
                    print(f"[{unidade}] FONTE DA LISTAGEM: {first.url}")
                    break

            if not page_urls:
                print(f"[{unidade}] NENHUMA PAGINAÇÃO DISPONÍVEL")
                return {}, {}

            # Não duplica a primeira página: workers processam toda a lista.
            page_urls = list(dict.fromkeys(page_urls))[:scraper.MAX_PAGES]
            print(f"[{unidade}] TOTAL DE PÁGINAS A PROCESSAR: {len(page_urls)}")
            state = await context.storage_state()
        finally:
            await context.close()

    # Novo navegador assíncrono: os workers são contextos isolados que reutilizam
    # o estado autenticado, evitando abrir várias instâncias com o mesmo perfil.
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            channel="msedge", headless=False,
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--no-sandbox"],
        )
        try:
            sem = asyncio.Semaphore(LIST_CONCURRENCY)
            counter = {"n": 0}

            async def worker(url):
                async with sem:
                    counter["n"] += 1
                    return await fetch_listing_page(browser, url, state, unidade, counter["n"])

            results = await asyncio.gather(*(worker(u) for u in page_urls))
        finally:
            await browser.close()

    found = {}
    errors = 0
    for r in results:
        if r.get("error"):
            errors += 1
        found.update(r.get("contracts") or {})
    print(f"[{unidade}] LISTAGEM FINALIZADA: páginas={len(page_urls)} erros={errors} contratos_azuis={len(found)}")
    return found, {"pages": len(page_urls), "errors": errors}


# ----------------------------- detalhes -----------------------------------
_CURRENT_UNIT = None
_CURRENT_CFG = None
_original_open_page = scraper.open_page


def open_fast(page, url, u, n, wait=1300):
    target = min(max(0, int(wait)), 300)
    for tentativa in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(target)
            if is_login_url(page.url):
                cfg = _CURRENT_CFG or {}
                scraper.login(page, cfg.get("usuario"), cfg.get("senha"), u)
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(target)
            if not is_login_url(page.url):
                return True
        except Exception as exc:
            if tentativa == 1:
                print(f"[{u}] ERRO abrindo {url}: {exc}")
    return False


scraper.open_page = open_fast


def run_details(unidade, cfg, contracts):
    global _CURRENT_UNIT, _CURRENT_CFG
    _CURRENT_UNIT, _CURRENT_CFG = unidade, cfg
    profile = scraper.EDGE_PROFILE_BASE / unidade
    profile.mkdir(parents=True, exist_ok=True)
    from playwright.sync_api import sync_playwright
    out = []
    with sync_playwright() as pw:
        b = pw.chromium.launch_persistent_context(
            user_data_dir=str(profile), channel="msedge", headless=False,
            viewport={"width":1440,"height":1000},
            args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--no-sandbox"],
        )
        page = b.pages[0] if b.pages else b.new_page()
        try:
            scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
            reps = scraper.global_reps(page, unidade)
            print(f"[{unidade}] REPOSIÇÕES GLOBAIS CAPTURADAS: {len(reps)}")
            total = len(contracts)
            for i, cu in enumerate(contracts, 1):
                cid = contract_id(cu)
                print(f"[{unidade}] CONTRATO ATIVO {i}/{total}: {cid}")
                try:
                    item = scraper.contract_bundle(page, cid, unidade, reps)
                    if item:
                        out.append(item)
                except Exception as exc:
                    print(f"[{unidade}] ERRO CONTRATO {cid}: {exc}")
            return out
        finally:
            try: b.close()
            except Exception: pass


def main():
    print("=" * 80)
    print("SCRAPER CGD - PAGINAÇÃO PARALELA / SOMENTE AZUIS / SEM DUPLICAÇÃO")
    print("=" * 80)
    all_data = []
    for unidade in ("matriz", "filial"):
        cfg = scraper.CONFIG[unidade]
        try:
            found, meta = asyncio.run(collect_listing_parallel(unidade, cfg))
            contracts = list(found.values())[:scraper.MAX_CONTRACTS]
            print(f"[{unidade}] CONTRATOS ATIVOS PARA DETALHAMENTO: {len(contracts)}")
            if contracts:
                data = run_details(unidade, cfg, contracts)
                all_data.extend(data)
            else:
                print(f"[{unidade}] nenhum contrato azul foi reservado; unidade encerrada sem varreduras alternativas")
        except Exception as exc:
            print(f"[{unidade}] ERRO FATAL: {exc}")

    unique = {(a.get("unidade"), a.get("contrato")): a for a in all_data}
    all_data = list(unique.values())
    scraper.JSON_PATH.write_text(json.dumps(all_data, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 80)
    print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(all_data)}")
    print(f"MATRIZ: {sum(a.get('unidade') == 'matriz' for a in all_data)}")
    print(f"FILIAL: {sum(a.get('unidade') == 'filial' for a in all_data)}")
    print("=" * 80)
    if not all_data:
        raise SystemExit("Nenhum aluno foi capturado pelo CGD.")
    scraper.sync_supabase(all_data)


if __name__ == "__main__":
    main()
