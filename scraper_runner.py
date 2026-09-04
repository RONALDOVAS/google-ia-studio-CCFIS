"""Executor CGD: paginação paralela com poucos browsers persistentes."""
import os
from concurrent.futures import ProcessPoolExecutor, as_completed
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
import scraper

LISTING_WORKERS=max(1,int(os.getenv("CGD_LISTING_WORKERS","4")))
LISTING_PAGES=max(1,int(os.getenv("CGD_LISTING_PAGES","829")))
CHUNK=(LISTING_PAGES+LISTING_WORKERS-1)//LISTING_WORKERS
LISTING_PATHS={"/alunos","/relatorios/alunos","/relatorios/individuais/alunos-curso"}
NEXT_SELECTORS=['a[rel="next"]','button[rel="next"]','a[aria-label*="next" i]','button[aria-label*="next" i]','a[aria-label*="proxima" i]','button[aria-label*="proxima" i]','a:has-text("Próxima")','button:has-text("Próxima")','a:has-text("Proxima")','button:has-text("Proxima")','a:has-text("Next")','button:has-text("Next")','a:has-text("›")','button:has-text("›")']

def _param(url):
 q=parse_qs(urlparse(url).query,keep_blank_values=True)
 for k,v in q.items():
  if v and v[-1].isdigit() and k.lower() in {"page","pagina","p","page_number","pag","pageindex","page_index"}: return k
 for k,v in q.items():
  if v and v[-1].isdigit() and int(v[-1])>=1:return k
 return None

def _set(url,key,n):
 p=urlparse(url);q=parse_qs(p.query,keep_blank_values=True);q[key]=[str(n)];return urlunparse(p._replace(query=urlencode(q,doseq=True)))

def _learn(page):
 try:
  a=page.locator('a[href]')
  for i in range(min(a.count(),3000)):
   h=a.nth(i).get_attribute('href')
   if h:
    h=scraper.abs_url(page,h)
    if scraper.same_host(h) and _param(h):return _param(h)
 except Exception:pass
 before=page.url
 try:
  old=scraper.body(page)[:10000]
  for s in NEXT_SELECTORS:
   loc=page.locator(s)
   for i in range(loc.count()):
    e=loc.nth(i)
    if not e.is_visible() or (e.get_attribute('aria-disabled') or '').lower()=='true' or 'disabled' in (e.get_attribute('class') or '').lower():continue
    e.click();page.wait_for_timeout(250)
    if page.url!=before and _param(page.url):return _param(page.url)
    if scraper.body(page)[:10000]!=old:return None
 except Exception:pass
 return None

def _worker(args):
 unidade,urls,user,password=args;found={};errors=0
 try:
  with scraper.sync_playwright() as pw:
   browser=pw.chromium.launch(channel='msedge',headless=True)
   context=browser.new_context();page=context.new_page();scraper.login(page,user,password,unidade)
   for n,url in enumerate(urls,1):
    try:
     if scraper.open_page(page,url,unidade,f'pagina_{n}',100):scraper.collect_contracts(page,found)
     else:errors+=1
    except Exception as e:errors+=1;print(f'[{unidade}] WORKER_ERRO {url}: {e}')
    if n==1 or n%10==0 or n==len(urls):print(f'[{unidade}] WORKER_PROG {n}/{len(urls)} contratos={len(found)} erros={errors}')
   browser.close()
 except Exception as e:return unidade,list(found.values()),errors+1,str(e)
 return unidade,list(found.values()),errors,None

def optimized_discover_contracts(page,unidade,destino):
 user=scraper.CONFIG[unidade]['usuario'];password=scraper.CONFIG[unidade]['senha']
 base=destino if destino and scraper.same_host(destino) and urlparse(destino).path.rstrip('/').lower() in LISTING_PATHS else None
 if not base:
  if not scraper.open_page(page,scraper.CGD_URL,unidade,'inicio',150):return []
  for _,h in scraper.links(page):
   if urlparse(h).path.rstrip('/').lower() in LISTING_PATHS:base=h;break
 if not base or not scraper.open_page(page,base,unidade,'lista_base',150):return []
 key=_learn(page)
 if not key:raise RuntimeError(f'[{unidade}] PAGINACAO_NAO_URL: fallback serial bloqueado')
 print(f'[{unidade}] PAGINACAO PARALELA REAL: paginas={LISTING_PAGES} workers={LISTING_WORKERS} parametro={key}')
 urls=[_set(base,key,n) for n in range(1,LISTING_PAGES+1)]
 chunks=[urls[i:i+CHUNK] for i in range(0,len(urls),CHUNK)]
 found={};errors=0
 with ProcessPoolExecutor(max_workers=LISTING_WORKERS) as pool:
  fs=[pool.submit(_worker,(unidade,c,user,password)) for c in chunks]
  for i,f in enumerate(as_completed(fs),1):
   unit,contracts,err,exc=f.result();errors+=err
   for c in contracts:
    cid=scraper.contract_id(c)
    if cid:found[cid]=scraper.contract_url(cid)
   print(f'[{unit}] WORKER_FINALIZADO={i}/{len(fs)} contratos={len(found)} erros={errors}')
 print(f'[{unidade}] PAGINACAO FINALIZADA paginas={LISTING_PAGES} contratos={len(found)} erros={errors}')
 return list(found.values())[:scraper.MAX_CONTRACTS]

scraper.discover_contracts=optimized_discover_contracts
if __name__=='__main__':scraper.main()
