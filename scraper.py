import os
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin
from concurrent.futures import ProcessPoolExecutor, as_completed
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
CGD_URL = os.getenv("CGD_LOGIN_URL") or "https://app.cgd.com.br/"
CONFIG = {
    "matriz": {"usuario": os.getenv("CGD_USER_MATRIZ"), "senha": os.getenv("CGD_PASS_MATRIZ"), "destino": os.getenv("CGD_MATRIZ_URL")},
    "filial": {"usuario": os.getenv("CGD_USER_FILIAL"), "senha": os.getenv("CGD_PASS_FILIAL"), "destino": os.getenv("CGD_FILIAL_URL")},
}
JSON_PATH = Path("dados_alunos.json")
DIAGNOSTICO_DIR = Path("diagnostico_scraping")
DIAGNOSTICO_DIR.mkdir(parents=True, exist_ok=True)
EDGE_PROFILE_BASE = Path(os.getenv("EDGE_PROFILE_DIR") or "edge_cgd_profiles")
MAX_CONTRACTS = int(os.getenv("CGD_MAX_CONTRACTS", "5000"))
MAX_PAGES = int(os.getenv("CGD_MAX_LINK_PAGES", "300"))
DETAIL_WORKERS = max(1, int(os.getenv("CGD_DETAIL_WORKERS", "4")))
PAGE_WAIT_MS = max(0, int(os.getenv("CGD_PAGE_WAIT_MS", "500")))
PAGE_TIMEOUT_MS = max(10000, int(os.getenv("CGD_PAGE_TIMEOUT_MS", "30000")))
DIAGNOSTICO = os.getenv("CGD_DIAGNOSTICO", "0").lower() in ("1", "true", "yes", "sim")
HEADLESS = os.getenv("CGD_HEADLESS", "0").lower() in ("1", "true", "yes", "sim")


def norm(v):
    return " ".join(str(v or "").replace("\xa0", " ").split())


def low(v):
    return norm(v).lower()


def abs_url(page, href):
    return urljoin(page.url, href or "").split("#", 1)[0]


def same_host(url):
    try:
        return urlparse(url).netloc == urlparse(CGD_URL).netloc
    except Exception:
        return False


def dump(page, u, n):
    if not DIAGNOSTICO:
        return
    try:
        s = re.sub(r"[^a-zA-Z0-9_-]+", "_", n.lower())
        (DIAGNOSTICO_DIR / f"{u}_{s}.html").write_text(page.content(), encoding="utf-8")
        (DIAGNOSTICO_DIR / f"{u}_{s}.txt").write_text(norm(page.locator("body").inner_text())[:120000], encoding="utf-8")
        page.screenshot(path=str(DIAGNOSTICO_DIR / f"{u}_{s}.png"), full_page=True)
    except Exception:
        pass


def open_page(page, url, u, n, wait=None):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(PAGE_WAIT_MS if wait is None else wait)
        print(f"[{u}] {n}: {page.url}")
        dump(page, u, n)
        return same_host(page.url)
    except Exception as e:
        print(f"[{u}] ERRO abrindo {url}: {e}")
        return False


def links(page):
    out = []
    try:
        a = page.locator("a")
        for i in range(min(a.count(), 10000)):
            try:
                e = a.nth(i)
                h = abs_url(page, e.get_attribute("href"))
                if h and same_host(h):
                    out.append((norm(e.inner_text()), h))
            except Exception:
                pass
    except Exception:
        pass
    seen, result = set(), []
    for t, u in out:
        if u not in seen:
            seen.add(u)
            result.append((t, u))
    return result


def contract_id(url):
    m = re.search(r"/contratos/(\d+)$", urlparse(url).path.rstrip("/"), re.I)
    return m.group(1) if m else None


def student_id(url):
    m = re.search(r"/alunos/(\d+)", urlparse(url).path, re.I)
    return m.group(1) if m else None


def contract_url(cid):
    return f"{CGD_URL.rstrip('/')}/contratos/{cid}"


def child_url(cid, k):
    return f"{CGD_URL.rstrip('/')}/contratos/{k}/{cid}"


def is_contract(url):
    return bool(re.fullmatch(r"/contratos/\d+", urlparse(url).path.rstrip("/"), re.I))


def table_data(page):
    out = []
    try:
        ts = page.locator("table")
        for i in range(ts.count()):
            t = ts.nth(i)
            heads = [norm(x) for x in t.locator("thead th").all_text_contents()]
            if not heads:
                heads = [norm(x) for x in t.locator("tr:first-child th,tr:first-child td").all_text_contents()]
            trs = t.locator("tbody tr")
            start = 0
            if trs.count() == 0:
                trs = t.locator("tr")
                start = 1 if trs.count() else 0
            rows = []
            for j in range(start, trs.count()):
                vals = [norm(x) for x in trs.nth(j).locator("td").all_text_contents()]
                if vals:
                    rows.append(vals)
            out.append((heads, rows))
    except Exception:
        pass
    return out


def col(heads, *names):
    names = tuple(low(x) for x in names)
    for i, h in enumerate(heads):
        if any(n in low(h) for n in names):
            return i
    return None


def body(page):
    try:
        return norm(page.locator("body").inner_text())
    except Exception:
        return ""


def extract_name(page, fallback=None):
    try:
        for sel in ('input[name*="nome" i]', 'input[id*="nome" i]'):
            loc = page.locator(sel)
            for i in range(loc.count()):
                v = norm(loc.nth(i).input_value())
                if len(v) >= 3 and len(v.split()) >= 2:
                    return v
    except Exception:
        pass
    for pat in (r"(?:Nome completo|Nome do aluno|Aluno|Estudante)\s*[:\-]\s*([^\n|]{4,150})", r"\bNome\s*[:\-]\s*([^\n|]{4,150})"):
        m = re.search(pat, body(page), re.I)
        if m and len(norm(m.group(1))) >= 4:
            return norm(m.group(1))
    return fallback


def login(page, user, password, u):
    page.goto(CGD_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
    page.wait_for_timeout(1500)
    us = page.locator('input[type="text"],input[type="email"],input[name*="user" i],input[name*="login" i],input[name*="email" i]')
    ps = page.locator('input[type="password"],input[name*="senha" i],input[name*="password" i]')
    U = P = None
    for i in range(us.count()):
        if us.nth(i).is_visible():
            U = us.nth(i)
            break
    for i in range(ps.count()):
        if ps.nth(i).is_visible():
            P = ps.nth(i)
            break
    if U and P and user and password:
        U.fill(user)
        P.fill(password)
        bs = page.locator('button[type="submit"],input[type="submit"],button:has-text("Entrar"),button:has-text("Acessar"),button:has-text("Login")')
        B = next((bs.nth(i) for i in range(bs.count()) if bs.nth(i).is_visible()), None)
        if not B:
            raise RuntimeError(f"[{u}] botao de login nao encontrado")
        B.click()
        page.wait_for_timeout(2000)
    if not same_host(page.url):
        raise RuntimeError(f"[{u}] login nao permaneceu no host do CGD: {page.url}")
    print(f"[{u}] LOGIN OK: {page.url}")
    dump(page, u, "apos_login")


def collect_contracts(page, found):
    for _, h in links(page):
        if is_contract(h):
            cid = contract_id(h)
            if cid:
                found[cid] = contract_url(cid)
    try:
        loc = page.locator('[href*="/contratos/"]')
        for i in range(min(loc.count(), 10000)):
            h = abs_url(page, loc.nth(i).get_attribute("href"))
            if is_contract(h):
                cid = contract_id(h)
                if cid:
                    found[cid] = contract_url(cid)
    except Exception:
        pass


def next_page(page):
    sels = ['a[rel="next"]','button[rel="next"]','a[aria-label*="next" i]','button[aria-label*="next" i]','a[aria-label*="proxima" i]','button[aria-label*="proxima" i]','a:has-text("Próxima")','button:has-text("Próxima")','a:has-text("Proxima")','button:has-text("Proxima")','a:has-text("Next")','button:has-text("Next")','a:has-text("›")','button:has-text("›")']
    before = body(page)[:8000]
    for sel in sels:
        try:
            loc = page.locator(sel)
            for i in range(loc.count()):
                e = loc.nth(i)
                if not e.is_visible():
                    continue
                if (e.get_attribute("aria-disabled") or "").lower() == "true" or "disabled" in (e.get_attribute("class") or "").lower():
                    continue
                e.click()
                page.wait_for_timeout(PAGE_WAIT_MS)
                if body(page)[:8000] != before:
                    return True
        except Exception:
            pass
    return False


def discover_contracts(page, u, destino):
    found = {}
    if destino and same_host(destino):
        open_page(page, destino, u, "rota_configurada")
        collect_contracts(page, found)
    open_page(page, CGD_URL, u, "inicio")
    sources = []
    for _, h in links(page):
        p = urlparse(h).path.rstrip("/").lower()
        if p in ("/alunos", "/relatorios/alunos", "/relatorios/individuais/alunos-curso"):
            sources.append(h)
    for src in dict.fromkeys(sources):
        if len(found) >= MAX_CONTRACTS:
            break
        open_page(page, src, u, "lista_alunos", 800)
        seen = set()
        for pn in range(1, MAX_PAGES + 1):
            collect_contracts(page, found)
            sig = body(page)[:12000]
            if sig in seen:
                break
            seen.add(sig)
            print(f"[{u}] pagina_lista={pn} contratos_acumulados={len(found)}")
            if not next_page(page):
                break
    contracts = list(found.values())[:MAX_CONTRACTS]
    print(f"[{u}] CONTRATOS UNICOS DESCOBERTOS: {len(contracts)}")
    print(f"[{u}] DETAIL_WORKERS: {DETAIL_WORKERS}")
    return contracts


def extract_frequency(page, cid):
    rec, faltas, pres = [], 0, 0
    for heads, rows in table_data(page):
        si = col(heads, "status", "situação", "situacao", "presença", "presenca", "frequência", "frequencia")
        di = col(heads, "data", "dia")
        ai = col(heads, "aluno", "nome", "estudante")
        for row in rows:
            s = low(row[si]) if si is not None and si < len(row) else ""
            if any(x in s for x in ("falta", "ausente", "não compareceu", "nao compareceu")):
                faltas += 1
            elif any(x in s for x in ("presente", "presença", "presenca", "compareceu")):
                pres += 1
            rec.append({"data": row[di] if di is not None and di < len(row) else None, "status": row[si] if si is not None and si < len(row) else None, "aluno": row[ai] if ai is not None and ai < len(row) else None, "valores": row, "cabecalhos": heads})
    return {"faltas": faltas, "presencas": pres, "registros": rec}


def extract_disciplines(page, src):
    out = []
    for heads, rows in table_data(page):
        if not any(x in low(" ".join(heads)) for x in ("disciplina", "módulo", "modulo", "passo", "etapa", "progresso", "carga horária", "carga horaria", "status")):
            continue
        for row in rows:
            r = {"disciplina": None, "modulo": None, "passo": None, "progresso": None, "carga_horaria": None, "data": None, "status": None, "cabecalhos": heads, "valores": row, "origem": src}
            for k, n in {"disciplina": ("disciplina",), "modulo": ("módulo", "modulo"), "passo": ("passo", "etapa"), "progresso": ("progresso",), "carga_horaria": ("carga horária", "carga horaria", "carga"), "data": ("data", "última", "ultima"), "status": ("status", "situação", "situacao", "estado")}.items():
                i = col(heads, *n)
                if i is not None and i < len(row):
                    r[k] = row[i]
            out.append(r)
    txt = body(page)
    ms = list(re.finditer(r"M[oó]dulo\s*(\d+)\b", txt, re.I))
    for i, m in enumerate(ms):
        chunk = txt[m.start():ms[i + 1].start() if i + 1 < len(ms) else min(len(txt), m.end() + 1000)]
        sm = re.search(r"(?:Passo|Etapa)\s*(\d+)\b", chunk, re.I)
        pm = re.search(r"(\d{1,3})\s*%", chunk)
        dm = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", chunk)
        out.append({"disciplina": None, "modulo": m.group(1), "passo": sm.group(1) if sm else None, "progresso": pm.group(1) + "%" if pm else None, "carga_horaria": None, "data": dm.group(1) if dm else None, "status": None, "texto_contexto": chunk[:3000], "cabecalhos": [], "valores": [], "origem": src})
    return out


def classify(rows):
    seen, r = set(), []
    for x in rows:
        k = json.dumps({q: x.get(q) for q in ("disciplina", "modulo", "passo", "progresso", "data", "origem")}, ensure_ascii=False, sort_keys=True)
        if k not in seen:
            seen.add(k)
            r.append(x)
    done, cur, fut = [], [], []
    for x in r:
        s = low(" ".join(str(x.get(k) or "") for k in ("status", "progresso", "texto_contexto", "valores")))
        p = low(x.get("progresso"))
        if "100%" in p or any(q in s for q in ("concluída", "concluida", "concluído", "concluido", "finalizada", "finalizado")):
            done.append(x)
        elif any(q in s for q in ("não inici", "nao inici", "aguardando", "futura", "não começou", "nao comecou")):
            fut.append(x)
        elif any(q in s for q in ("andamento", "em curso", "iniciad", "progresso")) or x.get("modulo") or x.get("passo"):
            cur.append(x)
    return r, done, cur, fut


def extract_replacements(page, u):
    out = []
    for heads, rows in table_data(page):
        if any(x in low(" ".join(heads)) for x in ("reposição", "reposicao", "contrato", "aluno", "data")):
            for row in rows:
                out.append({"cabecalhos": heads, "valores": row, "unidade": u})
    return out


def belongs(r, cid, sid, name):
    raw = low(" ".join(str(x) for x in r.get("valores", [])))
    return any(v and low(v) in raw for v in (cid, sid, name))


def contract_bundle(page, cid, u, reps):
    print(f"[{u}] >>> PROCESSANDO CONTRATO {cid}")
    cu = contract_url(cid)
    open_page(page, cu, u, f"contrato_{cid}")
    ctext = body(page)
    sl = [h for _, h in links(page) if student_id(h)]
    sid = student_id(sl[0]) if sl else None
    course = child_url(cid, "cursos")
    schedule = child_url(cid, "horarios")
    frequrl = child_url(cid, "frequencias")
    rows, st, name = [], "", None
    freq = {"faltas": 0, "presencas": 0, "registros": []}
    if open_page(page, course, u, f"cursos_individuais_{cid}"):
        rows += extract_disciplines(page, course)
    if open_page(page, schedule, u, f"horarios_individuais_{cid}"):
        st = body(page)[:20000]
    if open_page(page, frequrl, u, f"frequencia_{cid}"):
        freq = extract_frequency(page, cid)
        name = extract_name(page)
    if not sid:
        m = re.search(r"/alunos/(\d+)", ctext)
        sid = m.group(1) if m else None
    if sid and open_page(page, f"{CGD_URL.rstrip('/')}/alunos/{sid}/edit", u, f"aluno_{sid}"):
        name = extract_name(page, name)
        at = body(page)[:25000]
    else:
        at = ""
    rows, done, cur, fut = classify(rows)
    def num(r, k):
        m = re.search(r"\d+", str(r.get(k) or ""))
        return int(m.group()) if m else -1
    point = max(cur, key=lambda r: (num(r, "modulo"), num(r, "passo"), num(r, "progresso"))) if cur else None
    aluno = {
        "cgd_matricula_id": cid, "nome": name or f"Contrato {cid}", "contrato": cid, "email": None, "telefone": None,
        "curso": None, "turma_nome": None, "professor_nome": None, "data_inicio": None, "meses_contrato_total": None,
        "ultima_aula": None, "ultimo_acesso": None, "faltas_totais": freq["faltas"], "faltas_mes_atual": 0,
        "mes_referencia_faltas": datetime.now().strftime("%m/%Y"), "dias_em_curso": 0, "criticidade": "normal",
        "tratativa_sugerida": "normal", "status_tratativa": "pendente", "status_matricula": "ativo",
        "bloqueado_automaticamente": freq["faltas"] > 3, "motivo_bloqueio": "mais_de_3_faltas" if freq["faltas"] > 3 else None,
        "total_disciplinas_grade": len(rows), "disciplinas_concluidas": len(done), "unidade": u, "rota_cgd": cu,
        "rota_frequencia_cgd": frequrl, "rota_cursos_individuais_cgd": course, "rota_horarios_individuais_cgd": schedule,
        "rota_aluno_cgd": f"{CGD_URL.rstrip('/')}/alunos/{sid}/edit" if sid else None,
        "frequencia_detalhada": freq["registros"], "reposicoes_detalhadas": [r for r in reps if belongs(r, cid, sid, name)],
        "disciplinas_detalhadas": rows, "disciplinas_concluidas_detalhadas": done,
        "disciplinas_em_andamento_detalhadas": cur, "disciplinas_futuras_detalhadas": fut,
        "disciplina_atual": point.get("disciplina") if point else None, "modulo_atual": point.get("modulo") if point else None,
        "passo_atual": point.get("passo") if point else None, "data_ponto_atual": point.get("data") if point else None,
        "progresso_atual": point.get("progresso") if point else None, "carga_horaria_atual": point.get("carga_horaria") if point else None,
        "horarios_detalhados": st, "dados_aluno_detalhados": at,
    }
    print(f"[{u}] CAPTURADO: contrato={cid} aluno={aluno['nome']!r} aluno_id={sid} faltas={freq['faltas']} disciplinas={len(rows)} concluidas={len(done)} andamento={len(cur)} futuras={len(fut)}")
    return aluno


def global_reps(page, u):
    open_page(page, CGD_URL, u, "inicio_reposicoes")
    cs = [(t, h) for t, h in links(page) if "reposi" in low(t + " " + h) and "turmas/reposicao" not in h.lower()]
    if not cs:
        cs = [(t, h) for t, h in links(page) if "reposi" in low(t + " " + h)]
    if not cs:
        return []
    print(f"[{u}] REPOSICAO REAL: {cs[0][0]!r} -> {cs[0][1]}")
    if not open_page(page, cs[0][1], u, "individuais_reposicoes"):
        return []
    r = extract_replacements(page, u)
    print(f"[{u}] REPOSICOES GLOBAIS CAPTURADAS: {len(r)}")
    return r


def detail_worker(args):
    u, cfg, cid, reps, storage_state, attempt = args
    with sync_playwright() as pw:
        browser = None
        context = None
        try:
            browser = pw.chromium.launch(channel="msedge", headless=HEADLESS, args=["--disable-blink-features=AutomationControlled"])
            context = browser.new_context(storage_state=storage_state, viewport={"width": 1440, "height": 1000})
            page = context.new_page()
            page.set_default_timeout(PAGE_TIMEOUT_MS)
            page.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
            if not same_host(page.url or CGD_URL):
                page.goto(CGD_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            result = contract_bundle(page, cid, u, reps)
            return {"ok": True, "aluno": result, "cid": cid, "attempt": attempt}
        except Exception as e:
            print(f"[{u}] ERRO CONTRATO {cid} tentativa={attempt}: {e}")
            return {"ok": False, "cid": cid, "error": str(e), "attempt": attempt}
        finally:
            try:
                if context:
                    context.close()
            except Exception:
                pass
            try:
                if browser:
                    browser.close()
            except Exception:
                pass


def process_details(u, cfg, contracts, reps, storage_state):
    if not contracts:
        return []
    workers = min(DETAIL_WORKERS, len(contracts))
    print(f"[{u}] INICIO DETALHAMENTO PARALELO: {len(contracts)} contratos / {workers} workers")
    pending = list(contracts)
    results, failed = [], []
    for round_no in (1, 2):
        if not pending:
            break
        print(f"[{u}] LOTE DE DETALHAMENTO {round_no}: {len(pending)} contratos")
        args = [(u, cfg, contract_id(cu), reps, storage_state, round_no) for cu in pending]
        pending_next = []
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(detail_worker, a) for a in args]
            for idx, fut in enumerate(as_completed(futures), 1):
                try:
                    r = fut.result()
                except Exception as e:
                    r = {"ok": False, "cid": "desconhecido", "error": str(e), "attempt": round_no}
                if r.get("ok"):
                    results.append(r["aluno"])
                else:
                    pending_next.append(r.get("cid"))
                if idx % max(1, workers) == 0 or idx == len(futures):
                    print(f"[{u}] PROGRESSO DETALHAMENTO: {idx}/{len(futures)} concluídos nesta rodada")
        pending = [contract_url(cid) for cid in pending_next if cid]
        if pending:
            print(f"[{u}] RETENTATIVA NECESSARIA: {len(pending)} contratos")
    failed = pending
    print(f"[{u}] DETALHAMENTO FINALIZADO: sucesso={len(results)} falhas={len(failed)} de={len(contracts)}")
    for cu in failed:
        print(f"[{u}] CONTRATO NAO CAPTURADO APOS RETENTATIVA: {cu}")
    return results


def run_unit(u, cfg, pw):
    profile = EDGE_PROFILE_BASE / u
    profile.mkdir(parents=True, exist_ok=True)
    b = pw.chromium.launch_persistent_context(user_data_dir=str(profile), channel="msedge", headless=HEADLESS, viewport={"width": 1440, "height": 1000}, args=["--disable-blink-features=AutomationControlled"])
    page = b.pages[0] if b.pages else b.new_page()
    try:
        page.set_default_timeout(PAGE_TIMEOUT_MS)
        page.set_default_navigation_timeout(PAGE_TIMEOUT_MS)
        login(page, cfg["usuario"], cfg["senha"], u)
        contracts = discover_contracts(page, u, cfg["destino"])
        reps = global_reps(page, u)
        state = Path(f".cgd_storage_{u}.json")
        b.storage_state(path=str(state))
        b.close()
        b = None
        out = process_details(u, cfg, contracts, reps, str(state))
        try:
            state.unlink(missing_ok=True)
        except Exception:
            pass
        return out
    finally:
        try:
            if b:
                b.close()
        except Exception:
            pass


def sync_supabase(alunos):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("SUPABASE: nao configurado; JSON salvo.")
        return
    columns = {"cgd_matricula_id", "nome", "contrato", "email", "telefone", "curso", "turma_nome", "professor_nome", "data_inicio", "meses_contrato_total", "ultima_aula", "ultimo_acesso", "faltas_totais", "faltas_mes_atual", "mes_referencia_faltas", "dias_em_curso", "criticidade", "tratativa_sugerida", "status_tratativa", "status_matricula", "bloqueado_automaticamente", "motivo_bloqueio", "total_disciplinas_grade", "disciplinas_concluidas", "unidade", "rota_cgd"}
    try:
        client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        payload = [{k: v for k, v in a.items() if k in columns} for a in alunos]
        if payload:
            client.table("alunos").upsert(payload, on_conflict="contrato,unidade").execute()
            print(f"SUPABASE: {len(payload)} alunos sincronizados.")
    except Exception as e:
        print(f"SUPABASE: falha na sincronizacao: {e}")


def main():
    print("=" * 80)
    print("SCRAPER CGD - COLETA REAL COMPLETA POR UNIDADE / ALUNO")
    print("Fluxo: descoberta real -> reposicoes -> detalhamento paralelo por contrato")
    print(f"Configuracao: workers={DETAIL_WORKERS}, page_wait_ms={PAGE_WAIT_MS}, timeout_ms={PAGE_TIMEOUT_MS}, diagnostico={DIAGNOSTICO}")
    print("=" * 80)
    all_alunos = []
    with sync_playwright() as pw:
        for u in ("matriz", "filial"):
            try:
                all_alunos += run_unit(u, CONFIG[u], pw)
            except Exception as e:
                print(f"[{u}] ERRO FATAL: {e}")
    unique = {(a.get("unidade"), a.get("contrato")): a for a in all_alunos}
    all_alunos = list(unique.values())
    JSON_PATH.write_text(json.dumps(all_alunos, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 80)
    print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(all_alunos)}")
    print(f"MATRIZ: {sum(a.get('unidade') == 'matriz' for a in all_alunos)}")
    print(f"FILIAL: {sum(a.get('unidade') == 'filial' for a in all_alunos)}")
    print("=" * 80)
    if not all_alunos:
        raise SystemExit("Nenhum aluno foi capturado pelo CGD.")
    sync_supabase(all_alunos)


if __name__ == "__main__":
    main()
