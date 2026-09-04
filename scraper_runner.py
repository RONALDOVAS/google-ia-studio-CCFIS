"""Executor CGD: descoberta de paginação sem travar o navegador e coleta em workers."""
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import scraper

# Poucos workers de propósito: o runner é Windows e o Edge já é pesado.
LISTING_WORKERS=max(1,int(os.getenv("CGD_LISTING_WORKERS","2")))
LISTING_PAGES=max(1,int(os.getenv("CGD_LISTING_PAGES","829")))
LISTING_PATHS={"/alunos","/relatorios/alunos","/relatorios/individuais/alunos-curso"}
PREFERRED={"page","pagina","p","page_number","pag","pageindex","page_index"}


def _param_from_href(href):
    try:
        q=parse_qs(urlparse(href).query,keep_blank_values=True)
        for k,v in q.items():
            if v and v[-1].isdigit() and k.lower() in PREFERRED:
                return k
        for k,v in q.items():
            if v and v[-1].isdigit() and int(v[-1])>=1:
                return k
    except Exception:
        pass
    return None


def _set_page(url,key,n):
    p=urlparse(url); q=parse_qs(p.query,keep_blank_values=True); q[key]=[str(n)]
    return urlunparse(p._replace(query=urlencode(q,doseq=True)))


def _learn_parameter(page):
    """Inspeciona somente hrefs que já contêm parâmetros; não itera milhares de nós."""
    try:
        hrefs=page.locator('a[href]').evaluate_all("els => els.map(e => e.href).filter(Boolean)")
        for href in hrefs:
            if scraper.same_host(href):
                key=_param_from_href(href)
                if key:
                    return key
    except Exception as exc:
        print(f"[PAGINACAO] erro lendo hrefs: {exc}")
    return None


def _worker(args):
    unidade,urls,user,password=args
    found={}; errors=0
    try:
        with scraper.sync_playwright() as pw:
            browser=pw.chromium.launch(channel="msedge",headless=True)
            context=browser.new_context()
            page=context.new_page()
            scraper.login(page,user,password,unidade)
            for n,url in enumerate(urls,1):
                try:
                    if scraper.open_page(page,url,unidade,f"pagina_{n}",50):
                        scraper.collect_contracts(page,found)
                    else:
                        errors+=1
                except Exception as exc:
                    errors+=1
                    print(f"[{unidade}] WORKER_ERRO {url}: {exc}")
                if n==1 or n%25==0 or n==len(urls):
                    print(f"[{unidade}] WORKER_PROG {n}/{len(urls)} contratos={len(found)} erros={errors}")
            browser.close()
    except Exception as exc:
        return unidade,list(found.values()),errors+1,str(exc)
    return unidade,list(found.values()),errors,None


def optimized_discover_contracts(page,unidade,destino):
    user=scraper.CONFIG[unidade]["usuario"]
    password=scraper.CONFIG[unidade]["senha"]
    base=destino if destino and scraper.same_host(destino) and urlparse(destino).path.rstrip("/").lower() in LISTING_PATHS else None
    if not base:
        if not scraper.open_page(page,scraper.CGD_URL,unidade,"inicio",150):
            return []
        for _,href in scraper.links(page):
            if urlparse(href).path.rstrip("/").lower() in LISTING_PATHS:
                base=href; break
    if not base or not scraper.open_page(page,base,unidade,"lista_base",100):
        return []

    # NÃO clica em Próxima. Esse clique era o ponto que podia deixar a primeira
    # página presa. Descobrimos o parâmetro somente pelo DOM já carregado.
    key=_learn_parameter(page)
    if not key:
        raise RuntimeError(f"[{unidade}] PAGINACAO_NAO_IDENTIFICADA: sem fallback serial e sem clique de Next")

    print(f"[{unidade}] PAGINACAO PARALELA REAL: paginas={LISTING_PAGES} workers={LISTING_WORKERS} parametro={key}")
    urls=[_set_page(base,key,n) for n in range(1,LISTING_PAGES+1)]
    chunks=[urls[i::LISTING_WORKERS] for i in range(LISTING_WORKERS)]
    chunks=[c for c in chunks if c]
    found={}; errors=0
    with ProcessPoolExecutor(max_workers=LISTING_WORKERS) as pool:
        fs=[pool.submit(_worker,(unidade,c,user,password)) for c in chunks]
        for i,f in enumerate(as_completed(fs),1):
            unit,contracts,err,exc=f.result(); errors+=err
            for c in contracts:
                cid=scraper.contract_id(c)
                if cid: found[cid]=scraper.contract_url(cid)
            if exc: print(f"[{unit}] WORKER_FATAL: {exc}")
            print(f"[{unit}] WORKER_FINALIZADO={i}/{len(fs)} contratos_acumulados={len(found)} erros={errors}")
    print(f"[{unidade}] PAGINACAO FINALIZADA paginas={LISTING_PAGES} contratos={len(found)} erros={errors}")
    return list(found.values())[:scraper.MAX_CONTRACTS]

scraper.discover_contracts=optimized_discover_contracts

if __name__=="__main__":
    scraper.main()
