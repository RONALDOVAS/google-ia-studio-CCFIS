"""Executor CGD: listagem rapida, autenticacao persistente e detalhes estaveis."""
import asyncio
import os
import re
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse, urljoin

import scraper
from playwright.async_api import async_playwright

scraper.MAX_PAGES = int(os.getenv("CGD_MAX_LINK_PAGES", "5000"))
scraper.MAX_CONTRACTS = int(os.getenv("CGD_MAX_CONTRACTS", "10000"))
scraper.dump = lambda page, unidade, nome: None

PAGE_WAIT_MS = int(os.getenv("CGD_PAGE_WAIT_MS", "220"))
LIST_WAIT_MS = int(os.getenv("CGD_LIST_WAIT_MS", "180"))
LIST_CONCURRENCY = max(2, min(6, int(os.getenv("CGD_LIST_CONCURRENCY", "4"))))
BASE = scraper.CGD_URL.rstrip("/")


def is_login_url(url):
    return "/login" in urlparse(str(url)).path.lower()


def normalize_url(page_url, href):
    return urljoin(page_url, href or "").split("#", 1)[0]


def contract_id(url):
    m = re.search(r"/contratos/(\d+)$", urlparse(url).path.rstrip("/"), re.I)
    return m.group(1) if m else None


def contract_url(cid):
    return f"{BASE}/contratos/{cid}"


def classify_item(item):
    row = " ".join(str(item.get(k) or "").lower() for k in ("rowClass","rowStyle","rowTitle","rowAria","rowColor"))
    anchor = " ".join(str(item.get(k) or "").lower() for k in ("class","style","title","aria"))
    if any(x in row for x in ("text-danger","bg-danger","danger","vermelho","red","inativo","inactive","cancelado","encerrado")):
        return "red"
    if any(x in row for x in ("text-primary","bg-primary","text-info","bg-info","azul","blue","ativo","active","vigente")):
        return "blue"
    if any(x in anchor for x in ("text-danger","bg-danger","danger","vermelho","red","inativo","inactive","cancelado","encerrado")):
        return "red"
    if any(x in anchor for x in ("text-primary","bg-primary","text-info","bg-info","azul","blue","ativo","active","vigente")):
        return "blue"
    for m in re.finditer(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)", str(item.get("rowColor") or "").lower()):
        r,g,b=map(int,m.groups())
        if r>=150 and r>=g*1.45 and r>=b*1.45 and g<=150 and b<=150: return "red"
        if b>=120 and b>=r*1.20 and b>=g*1.05: return "blue"
    return "unknown"


async def prepare_page(page):
    async def route_handler(route):
        if route.request.resource_type in ("image","media","font"):
            await route.abort()
        else:
            await route.continue_()
    await page.route("**/*", route_handler)


async def login_async(page, user, password, unidade):
    await page.goto(BASE + "/", wait_until="domcontentloaded", timeout=60000)
    await page.wait_for_timeout(2500)
    us=page.locator('input[type="text"],input[type="email"],input[name*="user" i],input[name*="login" i],input[name*="email" i]')
    ps=page.locator('input[type="password"],input[name*="senha" i],input[name*="password" i]')
    U=P=None
    for i in range(await us.count()):
        e=us.nth(i)
        if await e.is_visible(): U=e; break
    for i in range(await ps.count()):
        e=ps.nth(i)
        if await e.is_visible(): P=e; break
    if not U or not P: raise RuntimeError(f"[{unidade}] campos de login nao encontrados")
    await U.fill(user); await P.fill(password)
    bs=page.locator('button[type="submit"],input[type="submit"],button:has-text("Entrar"),button:has-text("Acessar"),button:has-text("Login")')
    B=None
    for i in range(await bs.count()):
        e=bs.nth(i)
        if await e.is_visible(): B=e; break
    if not B: raise RuntimeError(f"[{unidade}] botao de login nao encontrado")
    await B.click(); await page.wait_for_timeout(3500)
    if is_login_url(page.url): raise RuntimeError(f"[{unidade}] login nao autenticou: {page.url}")
    print(f"[{unidade}] LOGIN OK: {page.url}")


async def extract_contracts(page, unidade):
    items=await page.locator('a[href*="/contratos/"]').evaluate_all("""
        els => els.map(a => {
            const tr=a.closest('tr');
            const nodes=[tr,tr&&tr.parentElement].filter(Boolean);
            const attrs=p=>nodes.map(x=>x.getAttribute(p)).filter(Boolean).join(' ');
            const styles=nodes.map(x=>{const s=getComputedStyle(x);return [s.color,s.backgroundColor,s.borderColor].join(' ')}).join(' ');
            return {href:a.href,class:a.getAttribute('class')||'',style:a.getAttribute('style')||'',title:a.getAttribute('title')||'',aria:a.getAttribute('aria-label')||'',rowClass:attrs('class'),rowStyle:attrs('style'),rowTitle:attrs('title'),rowAria:attrs('aria-label'),rowColor:styles};
        })
    """)
    found={}; red=unknown=0
    for item in items or []:
        h=normalize_url(page.url,item.get("href")); cid=contract_id(h)
        if not cid or cid in found: continue
        c=classify_item(item)
        if c=="red": red+=1
        elif c=="blue": found[cid]=contract_url(cid)
        else: unknown+=1
    print(f"[{unidade}] {page.url}: azuis={len(found)} vermelhos={red} nao_identificados={unknown}")
    return found


async def pagination_urls(page, unidade):
    data=await page.locator("a[href],button").evaluate_all("""
        els=>els.map(e=>({text:(e.innerText||'').trim(),href:e.href||e.getAttribute('href')||'',aria:e.getAttribute('aria-label')||'',rel:e.getAttribute('rel')||'',cls:e.getAttribute('class')||''}))
    """)
    base=page.url; numbered={}; last_href=None
    for x in data:
        h=normalize_url(base,x.get("href")); t=str(x.get("text") or "").strip(); a=str(x.get("aria") or "").lower(); z=(t.lower()+" "+a)
        if not h or is_login_url(h): continue
        if re.fullmatch(r"\d{1,5}",t):
            n=int(t)
            if 1<=n<=scraper.MAX_PAGES: numbered[n]=h
        if any(k in z for k in ("última","ultima","last")): last_href=h
    if len(numbered)>=2:
        return [numbered[n] for n in sorted(numbered)]
    if last_href:
        q=parse_qs(urlparse(last_href).query)
        for key,vals in q.items():
            if vals and vals[0].isdigit() and 1<int(vals[0])<=scraper.MAX_PAGES:
                n=int(vals[0]); p=urlparse(last_href); urls=[]
                for no in range(1,n+1):
                    qq=parse_qs(p.query); qq[key]=[str(no)]
                    urls.append(urlunparse((p.scheme,p.netloc,p.path,p.params,urlencode(qq,doseq=True),p.fragment)))
                print(f"[{unidade}] PAGINACAO INFERIDA: {n} paginas via {key}")
                return urls
    # Detecta hrefs de proxima pagina mesmo quando nao ha botao Última.
    for x in data:
        h=normalize_url(base,x.get("href")); q=parse_qs(urlparse(h).query)
        for key,vals in q.items():
            if vals and vals[0].isdigit() and key.lower() in ("page","pagina","p","pageindex","currentpage") and int(vals[0])>=2:
                n=int(vals[0]); p=urlparse(h); urls=[]
                for no in range(1,min(scraper.MAX_PAGES,n+1)):
                    qq=parse_qs(p.query); qq[key]=[str(no)]
                    urls.append(urlunparse((p.scheme,p.netloc,p.path,p.params,urlencode(qq,doseq=True),p.fragment)))
                return urls
    return [base]


async def find_listing(page, unidade, cfg):
    # A rota /horarios foi uma tentativa nova e nao e a fonte que comprovadamente
    # funcionou antes. Voltamos às fontes que produziram as 829 páginas e deixamos
    # /horarios apenas como ultimo fallback.
    sources=[]
    destino=cfg.get("destino")
    if destino and destino not in sources: sources.append(destino)
    for path in ("/alunos","/relatorios/alunos","/relatorios/individuais/alunos-curso","/horarios"):
        u=BASE+path
        if u not in sources: sources.append(u)
    for src in sources:
        try:
            await page.goto(src,wait_until="domcontentloaded",timeout=45000)
            await page.wait_for_timeout(LIST_WAIT_MS)
            if is_login_url(page.url):
                print(f"[{unidade}] FONTE DESCARTADA (sessao): {src}")
                continue
            initial=await extract_contracts(page,unidade)
            pages=await pagination_urls(page,unidade)
            # Se a fonte ja tem contratos, ela e valida mesmo que a paginacao seja
            # escondida. Isso evita o erro da versao anterior que exigia >1 pagina.
            if initial or len(pages)>1:
                print(f"[{unidade}] FONTE VALIDADA: {page.url}")
                return pages, initial
            print(f"[{unidade}] FONTE SEM CONTRATOS: {src}")
        except Exception as e:
            print(f"[{unidade}] ERRO FONTE {src}: {e}")
    return [], {}


async def fetch_listing_page(context,url,unidade,sem):
    page=await context.new_page(); await prepare_page(page)
    try:
        await page.goto(url,wait_until="domcontentloaded",timeout=45000); await page.wait_for_timeout(PAGE_WAIT_MS)
        if is_login_url(page.url):
            return {"contracts":{},"error":"sessao_expirada"}
        c=await extract_contracts(page,unidade)
        print(f"[{unidade}] worker {sem}: pagina processada")
        return {"contracts":c,"error":None}
    except Exception as e:
        print(f"[{unidade}] worker {sem}: ERRO {url}: {e}")
        return {"contracts":{},"error":str(e)}
    finally:
        await page.close()


async def collect_listing_parallel(unidade,cfg):
    async with async_playwright() as pw:
        context=await pw.chromium.launch_persistent_context(
            user_data_dir=str(scraper.EDGE_PROFILE_BASE/unidade),channel="msedge",headless=False,
            viewport={"width":1440,"height":1000},args=["--disable-blink-features=AutomationControlled"],
            ignore_default_args=["--no-sandbox"])
        try:
            first=context.pages[0] if context.pages else await context.new_page(); await prepare_page(first)
            await login_async(first,cfg.get("usuario"),cfg.get("senha"),unidade)
            page_urls,initial=await find_listing(first,unidade,cfg)
            if not page_urls:
                return {},{"pages":0,"errors":1}
            page_urls=list(dict.fromkeys(page_urls))[:scraper.MAX_PAGES]
            print(f"[{unidade}] TOTAL DE PAGINAS A PROCESSAR: {len(page_urls)}")
            # A primeira página ja foi lida; se houver muitas páginas, ela sera
            # novamente lida por worker somente uma vez, sem duplicar contratos.
            sem=asyncio.Semaphore(LIST_CONCURRENCY); counter=0
            async def worker(u):
                nonlocal counter
                async with sem:
                    counter+=1
                    return await fetch_listing_page(context,u,unidade,counter)
            results=await asyncio.gather(*(worker(u) for u in page_urls))
            found=dict(initial); errors=0
            for r in results:
                if r.get("error"): errors+=1
                found.update(r.get("contracts") or {})
            print(f"[{unidade}] LISTAGEM FINALIZADA: paginas={len(page_urls)} erros={errors} contratos_azuis={len(found)}")
            return found,{"pages":len(page_urls),"errors":errors}
        finally:
            await context.close()


_CURRENT_CFG=None

def open_fast(page,url,u,n,wait=1300):
    target=min(max(0,int(wait)),300)
    for tentativa in range(2):
        try:
            page.goto(url,wait_until="domcontentloaded",timeout=45000); page.wait_for_timeout(target)
            if is_login_url(page.url):
                cfg=_CURRENT_CFG or {}; scraper.login(page,cfg.get("usuario"),cfg.get("senha"),u)
                page.goto(url,wait_until="domcontentloaded",timeout=45000); page.wait_for_timeout(target)
            return not is_login_url(page.url)
        except Exception as e:
            if tentativa: print(f"[{u}] ERRO abrindo {url}: {e}")
    return False

scraper.open_page=open_fast


def run_details(unidade,cfg,contracts):
    global _CURRENT_CFG
    _CURRENT_CFG=cfg
    profile=scraper.EDGE_PROFILE_BASE/unidade; profile.mkdir(parents=True,exist_ok=True)
    from playwright.sync_api import sync_playwright
    with sync_playwright() as pw:
        b=pw.chromium.launch_persistent_context(user_data_dir=str(profile),channel="msedge",headless=False,viewport={"width":1440,"height":1000},args=["--disable-blink-features=AutomationControlled"],ignore_default_args=["--no-sandbox"])
        page=b.pages[0] if b.pages else b.new_page(); out=[]
        try:
            scraper.login(page,cfg.get("usuario"),cfg.get("senha"),unidade)
            reps=scraper.global_reps(page,unidade)
            print(f"[{unidade}] REPOSICOES GLOBAIS CAPTURADAS: {len(reps)}")
            for i,cu in enumerate(contracts,1):
                cid=contract_id(cu); print(f"[{unidade}] CONTRATO ATIVO {i}/{len(contracts)}: {cid}")
                try:
                    item=scraper.contract_bundle(page,cid,unidade,reps)
                    if item: out.append(item)
                except Exception as e: print(f"[{unidade}] ERRO CONTRATO {cid}: {e}")
            print(f"[{unidade}] ALUNOS COLETADOS: {len(out)}")
            return out
        finally:
            try:b.close()
            except:pass


def main():
    print("="*80); print("SCRAPER CGD - FONTE COMPROVADA + PAGINACAO PARALELA + SEM DUPLICACAO"); print("="*80)
    all_data=[]
    for unidade in ("matriz","filial"):
        cfg=scraper.CONFIG[unidade]
        try:
            found,meta=asyncio.run(collect_listing_parallel(unidade,cfg))
            contracts=list(found.values())[:scraper.MAX_CONTRACTS]
            print(f"[{unidade}] CONTRATOS ATIVOS PARA DETALHAMENTO: {len(contracts)}")
            if contracts: all_data.extend(run_details(unidade,cfg,contracts))
            else: print(f"[{unidade}] nenhum contrato azul foi reservado")
        except Exception as e: print(f"[{unidade}] ERRO FATAL: {e}")
    unique={(a.get("unidade"),a.get("contrato")):a for a in all_data}; all_data=list(unique.values())
    scraper.JSON_PATH.write_text(__import__("json").dumps(all_data,ensure_ascii=False,indent=2),encoding="utf-8")
    print("="*80); print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(all_data)}"); print(f"MATRIZ: {sum(a.get('unidade')=='matriz' for a in all_data)}"); print(f"FILIAL: {sum(a.get('unidade')=='filial' for a in all_data)}"); print("="*80)
    if not all_data: raise SystemExit("Nenhum aluno foi capturado pelo CGD.")
    scraper.sync_supabase(all_data)

if __name__=="__main__": main()
