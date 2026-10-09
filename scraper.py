import os
import json
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin
from concurrent.futures import ProcessPoolExecutor, as_completed
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
CGD_URL = "https://app.cgd.com.br/"
CGD_LOGIN_URL = os.getenv("CGD_LOGIN_URL") or "https://app.cgd.com.br/login"
CONFIG = {
    "matriz": {"usuario": os.getenv("CGD_USER_MATRIZ"), "senha": os.getenv("CGD_PASS_MATRIZ"), "destino": os.getenv("CGD_MATRIZ_URL")},
    "filial": {"usuario": os.getenv("CGD_USER_FILIAL"), "senha": os.getenv("CGD_PASS_FILIAL"), "destino": os.getenv("CGD_FILIAL_URL")},
}
PROJECT_ROOT = Path(__file__).resolve().parent
JSON_PATH = PROJECT_ROOT / "dados_alunos.json"
DIAGNOSTICO_DIR = PROJECT_ROOT / "diagnostico_scraping"
DIAGNOSTICO_DIR.mkdir(parents=True, exist_ok=True)
EDGE_PROFILE_BASE = Path(os.getenv("EDGE_PROFILE_DIR") or str(PROJECT_ROOT / "edge_cgd_profiles"))
EDGE_PROFILE_BASE.mkdir(parents=True, exist_ok=True)
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


def page_is_blocked(page):
    text = low(body(page))
    return any(marker in text for marker in (
        "sorry, you have been blocked",
        "you have been blocked",
        "checking your browser",
        "just a moment",
        "why have i been blocked",
        "challenge-platform",
        "cf-chl-",
    ))

def open_page(page, url, u, n, wait=None):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        page.wait_for_timeout(PAGE_WAIT_MS if wait is None else wait)
        print(f"[{u}] {n}: {page.url}")
        if page_is_blocked(page):
            dump(page, u, f"{n}_CLOUDFLARE")
            raise RuntimeError(f"[{u}] CLOUDFLARE_OU_BLOQUEIO_DETECTADO rota={url}")
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
    m = re.search(r"/contratos/(\d+)", urlparse(url).path, re.I)
    return m.group(1) if m else None


def student_id(url):
    m = re.search(r"/alunos/(\d+)", urlparse(url).path, re.I)
    return m.group(1) if m else None


def contract_url(cid):
    return f"{CGD_URL.rstrip('/')}/contratos/{cid}"


def child_url(cid, k):
    # Rota real da frequência individual no CGD inclui /list.
    # Sem esse sufixo o contrato abre, mas a grade de frequência não é carregada.
    if k == "frequencias":
        return f"{CGD_URL.rstrip('/')}/contratos/frequencias/{cid}/list"
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
    try:
        html = page.content()
        m_link = re.search(r'<a\s+[^>]*href=["\'](?:https?://[^"\']+)?/alunos/\d+[^"\']*["\'][^>]*>\s*([^<]+?)\s*</a>', html, re.I)
        if m_link:
            cand = norm(m_link.group(1))
            if len(cand) >= 4 and len(cand.split()) >= 2 and not any(w in cand.lower() for w in ("aluno", "editar", "ver", "detalhes")):
                return cand
    except Exception:
        pass
    return extract_name_from_text(body(page)) or fallback


def extract_name_from_text(text):
    text = norm(text)
    if not text:
        return None
    patterns = (
        r"Contrato\s+(?:Hor[aá]rios\s+|Cursos\s+)?([A-Za-zÀ-ÿ\s]{4,60})\s+\d{1,2}\s+anos",
        r"(?:Nome\s+completo|Nome\s+do\s+aluno|Aluno|Estudante)\s*[:\-]?\s+([A-Za-zÀ-ÿ]{2,}(?:\s+[A-Za-zÀ-ÿ]{2,})+)",
    )
    for pat in patterns:
        m = re.search(pat, text, re.I)
        if m:
            cand = norm(m.group(1))
            if len(cand) >= 4 and len(cand.split()) >= 2:
                return cand
    return None


def extract_name_from_sources(page, *sources):
    candidates = []
    try:
        title = norm(page.title())
        if title:
            candidates.append(title)
    except Exception:
        pass
    try:
        headings = page.locator("h1,h2,h3")
        for i in range(min(headings.count(), 30)):
            text = norm(headings.nth(i).inner_text())
            if text:
                candidates.append(text)
    except Exception:
        pass
    candidates.extend(norm(x) for x in sources if norm(x))
    for source in candidates:
        value = extract_name_from_text(source)
        if value:
            return value
    return None

def login(page, user, password, u):
    page.goto(CGD_LOGIN_URL, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
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
    if not U or not P:
        raise RuntimeError(f"[{u}] CAMPOS_LOGIN_NAO_ENCONTRADOS: {page.url}")
    if not user or not password:
        raise RuntimeError(f"[{u}] CREDENCIAIS_NAO_CONFIGURADAS")
    U.fill(user)
    P.fill(password)
    bs = page.locator('button[type="submit"],input[type="submit"],button:has-text("Entrar"),button:has-text("Acessar"),button:has-text("Login")')
    B = next((bs.nth(i) for i in range(bs.count()) if bs.nth(i).is_visible()), None)
    if not B:
        raise RuntimeError(f"[{u}] botao de login nao encontrado")
    B.click()
    page.wait_for_timeout(2500)
    if not same_host(page.url):
        raise RuntimeError(f"[{u}] login saiu do host do CGD: {page.url}")
    final_path = urlparse(page.url).path.rstrip("/").lower()
    if final_path == "/login" or final_path.startswith("/login/"):
        raise RuntimeError(f"[{u}] LOGIN_REJEITADO_OU_SESSAO_NAO_ESTABELECIDA: {page.url}")
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


def _route_kind(text, href):
    hay = low(f"{text} {href}")
    if "frequenc" in hay:
        return "frequencia"
    if "horario" in hay or "agenda" in hay:
        return "horarios"
    if "disciplina" in hay or "curso" in hay:
        return "disciplinas"
    if "ocorr" in hay:
        return "ocorrencias"
    if "pend" in hay:
        return "pendencias"
    if "assin" in hay:
        return "assinaturas"
    if "document" in hay or "arquivo" in hay:
        return "documentos"
    if "pagamento" in hay or "finance" in hay or "financeiro" in hay:
        return "financeiro"
    if "turma" in hay or "/turmas/" in hay:
        return "turmas"
    if "nota" in hay:
        return "notas"
    if "histórico" in hay or "historico" in hay:
        return "historico"
    if "cadastro" in hay or "/alunos/" in hay:
        return "cadastro_aluno"
    if "tag" in hay:
        return "tags"
    if "imprimir" in hay or "impress" in hay or "certificado" in hay:
        return "imprimir_certificado"
    if "encerrar" in hay or "encerramento" in hay or "cancelar" in hay:
        return "encerrar_contrato"
    if "contrato" in hay:
        return "contrato"
    return "outra"


def _route_is_for_entity(url, cid, sid=None):
    """Accept only routes whose path identifies this contract or student."""
    if not url or not same_host(url):
        return False
    path = urlparse(url).path.rstrip("/").lower()
    segments = [segment for segment in path.split("/") if segment]
    if any(part in {"delete", "destroy", "excluir", "logout", "encerrar", "cancelar", "remover", "deletar", "salvar", "save", "update"} for part in segments):
        return False
    if path.startswith("/contratos/") and str(cid) in segments:
        return True
    if sid and re.match(r"^/alunos/" + re.escape(str(sid)) + r"(?:/|$)", path):
        return True
    return False


def discover_contract_routes(page, cid, sid=None):
    """Discover only routes directly tied to the current contract/student."""
    routes = []
    seen = set()
    if not sid:
        for _, href in links(page):
            found_sid = student_id(href)
            if found_sid:
                sid = found_sid
                break
    if not sid:
        try:
            match = re.search(r"/alunos/(\d+)", page.content(), re.I)
            sid = match.group(1) if match else None
        except Exception:
            sid = None

    def add(text, href, source="link"):
        href = abs_url(page, href)
        if not _route_is_for_entity(href, cid, sid):
            return
        key = href.split("#", 1)[0]
        if key in seen:
            return
        seen.add(key)
        routes.append({
            "texto": norm(text), "url": key, "rota": _route_kind(text, key),
            "origem": source, "contrato_id": str(cid),
            "aluno_id": str(sid) if sid else None,
        })

    for text, href in links(page):
        add(text, href, "link")
    try:
        loc = page.locator("[data-href],[data-url],[href],[onclick]")
        for i in range(min(loc.count(), 5000)):
            el = loc.nth(i)
            label = norm(el.inner_text())
            for attr in ("href", "data-href", "data-url"):
                value = el.get_attribute(attr)
                if value:
                    add(label, value, f"attribute:{attr}")
            onclick = el.get_attribute("onclick") or ""
            for match in re.findall(r"""['"]((?:https?://|/)[^'"]+)['"]""", onclick):
                add(label, match, "onclick")
    except Exception:
        pass
    return routes


def _safe_contract_route(route, cid, sid=None):
    return _route_is_for_entity(route.get("url") or "", cid, sid)


def _capture_route_snapshot(page, unidade, cid, route, index, sid=None):
    if not _safe_contract_route(route, cid, sid):
        return None
    if not open_page(page, route["url"], unidade, f"contrato_{cid}_rota_{index}", 500):
        return None
    tables = [{"cabecalhos": h, "linhas": r} for h, r in table_data(page)]
    text = body(page)
    return {
        "texto": route["texto"],
        "url": page.url,
        "rota": route["rota"],
        "origem": route["origem"],
        "tabelas": tables,
        "campos_dom": _extract_dom_fields(page),
        "texto_corpo": text[:60000],
    }



def _wait_ajax_route(page, label, max_wait_s=8):
    """Aguarda uma rota CGD dinâmica sair do estado de shell/carregando."""
    deadline = __import__("time").monotonic() + max_wait_s
    while __import__("time").monotonic() < deadline:
        texto = low(body(page))
        tabelas = table_data(page)
        tem_linhas = any(rows for _, rows in tabelas)
        if "carregando..." not in texto or tem_linhas:
            return
        page.wait_for_timeout(500)
    print(f"[AJAX] TIMEOUT_RENDER label={label} url={page.url}", flush=True)


def _campo_rotulado_texto(texto, rotulos):
    texto = norm(texto)
    for rotulo in rotulos:
        padroes = (
            rf"\b{re.escape(rotulo)}\s*[:\-]\s*([^|;\n]{{2,160}})",
            rf"\b{re.escape(rotulo)}\s+([^|;\n]{{2,160}})",
        )
        for padrao in padroes:
            m = re.search(padrao, texto, re.I)
            if m:
                valor = norm(m.group(1))
                valor = re.split(
                    r"\s+(?:Curso|Turma|Professor|Data|Status|Situa[cç][aã]o)\s*[:\-]?\s*",
                    valor, maxsplit=1, flags=re.I
                )[0]
                if valor:
                    return valor
    return None


def _key_norm(value):
    value = unicodedata.normalize("NFD", norm(value).lower())
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    return re.sub(r"[^a-z0-9]+", "_", value).strip("_")


def _extract_dom_fields(page):
    """Read labels, disabled controls, selects and key/value table rows."""
    aliases = {
        "curso": ("curso", "curso do aluno", "curso contratado", "nome do curso"),
        "turma": ("turma", "turma atual", "turma do aluno", "enturmacao"),
        "professor": ("professor", "professor responsavel", "professor titular"),
        "data_inicio": ("data de inicio", "inicio do contrato", "inicio da matricula", "data inicio"),
        "data_matricula": ("data de matricula", "matricula em"),
        "data_fim": ("data de termino", "termino do contrato", "data fim", "data final"),
        "status_matricula": ("status", "situacao", "situacao da matricula", "status da matricula"),
    }
    out = {}

    def field_for(label):
        label_norm = _key_norm(label).replace("_", " ")
        for field, names in aliases.items():
            if any(label_norm == name or label_norm.startswith(name + " ") for name in names):
                return field
        return None

    def value_of(loc):
        try:
            tag = str(loc.evaluate("(el) => el.tagName")).lower()
            if tag == "select":
                selected = loc.locator("option:checked")
                return norm(selected.first.inner_text()) if selected.count() else norm(loc.input_value())
            if tag in ("input", "textarea"):
                return norm(loc.input_value())
            return norm(loc.inner_text())
        except Exception:
            return ""

    def save(label, value):
        field = field_for(label)
        value = norm(value)
        if field and value and not out.get(field):
            out[field] = value

    try:
        labels = page.locator("label")
        for i in range(min(labels.count(), 500)):
            label = labels.nth(i)
            if not field_for(label.inner_text()):
                continue
            target = None
            target_id = label.get_attribute("for")
            if target_id:
                loc = page.locator("#" + target_id)
                if loc.count():
                    target = loc.first
            if target is None:
                parent = label.locator("xpath=..")
                for selector in ("input", "select", "textarea"):
                    loc = parent.locator(selector)
                    if loc.count():
                        target = loc.first
                        break
            if target is not None:
                save(label.inner_text(), value_of(target))
    except Exception:
        pass

    try:
        for selector in ("input", "select", "textarea"):
            controls = page.locator(selector)
            for i in range(min(controls.count(), 2000)):
                el = controls.nth(i)
                label = " ".join(filter(None, [
                    el.get_attribute("aria-label"), el.get_attribute("placeholder"),
                    el.get_attribute("name"), el.get_attribute("id")
                ]))
                save(label, value_of(el))
    except Exception:
        pass

    try:
        tables = page.locator("table")
        for ti in range(min(tables.count(), 100)):
            rows = tables.nth(ti).locator("tr")
            for i in range(min(rows.count(), 2000)):
                cells = rows.nth(i).locator("th,td")
                if cells.count() >= 2:
                    save(cells.nth(0).inner_text(), cells.nth(1).inner_text())
    except Exception:
        pass
    return out


def _json_payload_matches(payload, url, cid, sid=None):
    url_text = str(url or "")
    if str(cid) and re.search(r"(?<!\d)" + re.escape(str(cid)) + r"(?!\d)", url_text):
        return True
    if sid and re.search(r"(?<!\d)" + re.escape(str(sid)) + r"(?!\d)", url_text):
        return True
    target_ids = {str(cid)} | ({str(sid)} if sid else set())
    stack = [payload]
    while stack:
        item = stack.pop()
        if isinstance(item, dict):
            for key, value in item.items():
                k = _key_norm(key)
                if k in {"contrato", "contrato_id", "matricula", "matricula_id", "cgd_matricula_id", "student_id", "aluno_id", "contract_id"} and str(value) in target_ids:
                    return True
                if isinstance(value, (dict, list)):
                    stack.append(value)
        elif isinstance(item, list):
            stack.extend(item[:1000])
    return False


def _extract_json_domain_fields(payloads, cid, sid=None):
    aliases = {
        "curso": {"curso", "curso_nome", "nome_curso", "curso_contratado", "course", "course_name"},
        "turma": {"turma", "turma_nome", "nome_turma", "turma_atual", "class", "class_name"},
        "professor": {"professor", "professor_nome", "nome_professor", "professor_responsavel", "teacher", "teacher_name"},
        "data_inicio": {"data_inicio", "inicio", "inicio_matricula", "data_matricula", "data_inicio_contrato", "start_date", "enrollment_date"},
        "data_matricula": {"data_matricula", "matricula_em", "enrollment_date"},
        "data_fim": {"data_fim", "data_termino", "termino", "fim_contrato", "end_date"},
        "status_matricula": {"status_matricula", "situacao_matricula", "status", "situacao", "state"},
    }
    found = {}
    for item in payloads or []:
        if not isinstance(item, dict):
            continue
        payload, url = item.get("data"), item.get("url") or ""
        if not _json_payload_matches(payload, url, cid, sid):
            continue
        stack = [payload]
        while stack:
            obj = stack.pop()
            if isinstance(obj, dict):
                for key, value in obj.items():
                    k = _key_norm(key)
                    for field, names in aliases.items():
                        if k in names and not found.get(field) and isinstance(value, (str, int, float)):
                            value_text = norm(value)
                            if value_text:
                                found[field] = value_text
                    if isinstance(value, (dict, list)):
                        stack.append(value)
            elif isinstance(obj, list):
                stack.extend(obj[:1000])
    return found


def _parse_date_value(value):
    value = norm(value)
    if not value:
        return None
    match = re.search(r"\b(\d{4})-(\d{1,2})-(\d{1,2})\b", value)
    if match:
        y, m, d = map(int, match.groups())
    else:
        match = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", value)
        if not match:
            return None
        d, m, y = map(int, match.groups())
        if y < 100:
            y += 2000
    try:
        from datetime import date
        return date(y, m, d).isoformat()
    except ValueError:
        return None


def _apply_assignment_fallback(domain, evidence_text):
    evidence = unicodedata.normalize("NFD", low(evidence_text))
    evidence = "".join(ch for ch in evidence if unicodedata.category(ch) != "Mn")
    unallocated = any(marker in evidence for marker in (
        "sem turma", "nao enturmado", "pendente de enturmacao",
        "aguardando enturmacao", "sem professor", "nao alocado",
        "trancado", "desistente", "evadido"
    ))
    turma_value = _key_norm(domain.get("turma")).replace("_", " ")
    professor_value = _key_norm(domain.get("professor")).replace("_", " ")
    empty_turma = not turma_value or turma_value in {"-", "--", "selecione", "nao informado", "nao enturmado", "sem turma"}
    empty_professor = not professor_value or professor_value in {"-", "--", "selecione", "nao informado", "nao alocado", "sem professor"}
    if unallocated and empty_turma:
        domain["turma"] = "SEM TURMA"
    if unallocated and empty_professor:
        domain["professor"] = "NÃO ALOCADO"
    return domain


def _extrair_campos_dominio_browser(page, ctext, course_text, schedule_text, aluno_raw, snapshots,
                                     response_payloads=None, cid=None, sid=None):
    textos = [ctext, course_text, schedule_text, aluno_raw]
    tabelas, dom_fields = [], {}
    for snap in snapshots:
        textos.append(snap.get("texto_corpo") or "")
        for field, value in (snap.get("campos_dom") or {}).items():
            if value and not dom_fields.get(field):
                dom_fields[field] = value
        for table in snap.get("tabelas") or []:
            tabelas.append((table.get("cabecalhos") or [], table.get("linhas") or []))
    texto = " | ".join(norm(x) for x in textos if norm(x))
    for field, value in _extract_dom_fields(page).items():
        if value and not dom_fields.get(field):
            dom_fields[field] = value
    network = _extract_json_domain_fields(response_payloads or [], cid, sid)
    domain = {}
    for field in ("curso", "turma", "professor", "data_inicio", "data_matricula", "data_fim", "status_matricula"):
        value = network.get(field) or dom_fields.get(field)
        if not value and field in {"curso", "turma", "professor"}:
            labels = {
                "curso": ("Curso", "Curso do aluno", "Curso contratado"),
                "turma": ("Turma", "Turma atual", "Turma do aluno"),
                "professor": ("Professor", "Professor responsável", "Professor responsavel"),
            }[field]
            value = _campo_rotulado_texto(texto, labels) or _campo_tabela(tabelas, (field,))
        if not value and field in {"data_inicio", "data_matricula", "data_fim"}:
            labels = {
                "data_inicio": ("Data de início", "Data de inicio", "Início", "Inicio"),
                "data_matricula": ("Data de matrícula", "Data de matricula", "Matrícula", "Matricula"),
                "data_fim": ("Data de término", "Data de termino", "Término", "Termino", "Data fim"),
            }[field]
            value = _campo_rotulado_texto(texto, labels)
        domain[field] = value
    for field in ("data_inicio", "data_matricula", "data_fim"):
        domain[field] = _parse_date_value(domain.get(field))
    return _apply_assignment_fallback(domain, texto + " " + str(domain.get("status_matricula") or ""))


def _install_response_capture(page):
    """Capture XHR/Fetch JSON without adding one permanent listener per contract."""
    state = getattr(page, "_cgd_response_capture_state", None)
    if state is None:
        state = {"payloads": []}
        def on_response(response):
            try:
                payloads = state["payloads"]
                if not same_host(response.url) or response.request.resource_type not in ("xhr", "fetch"):
                    return
                content_type = (response.headers or {}).get("content-type", "").lower()
                if "json" not in content_type or len(payloads) >= 200:
                    return
                data = response.json()
                if isinstance(data, (dict, list)):
                    payloads.append({"url": response.url, "data": data})
            except Exception:
                pass
        page.on("response", on_response)
        page._cgd_response_capture_state = state
    else:
        state["payloads"] = []
    return state["payloads"]

def contract_bundle(page, cid, u, reps):
    print(f"[{u}] >>> PROCESSANDO CONTRATO {cid}")
    cu = contract_url(cid)
    response_payloads = _install_response_capture(page)
    open_page(page, cu, u, f"contrato_{cid}")
    ctext = body(page)
    sl = [h for _, h in links(page) if student_id(h)]
    sid = student_id(sl[0]) if sl else None
    if not sid:
        try:
            match = re.search(r"/alunos/(\d+)", page.content(), re.I)
            sid = match.group(1) if match else None
        except Exception:
            sid = None
    routes = discover_contract_routes(page, cid, sid)
    print(f"[{u}] ROTAS_CGD_ESPECIFICAS cid={cid} aluno={sid or 'nao_identificado'} total={len(routes)}", flush=True)
    rows, st, name = [], "", None
    course_text = ""
    schedule_text = ""
    freq = {"faltas": 0, "presencas": 0, "registros": []}
    route_snapshots = []
    visited = set()

    name = extract_name(page)
    for sel in ("h1, h2, h3, h4, .content-header, .box-title, .card-title, .breadcrumb li"):
        if name:
            break
        try:
            for t in page.locator(sel).all_inner_texts():
                cand = extract_name_from_text(t)
                if cand:
                    name = cand
                    break
        except Exception:
            pass

    # Primeiro, as três rotas conhecidas e necessárias. Elas recebem
    # tratamento específico porque seus campos alimentam o domínio do CFIS.
    known = [
        ("disciplinas", child_url(cid, "cursos")),
        ("horarios", child_url(cid, "horarios")),
        ("frequencia", child_url(cid, "frequencias")),
    ]
    for kind, url in known:
        route = {"texto": kind, "url": url, "rota": kind, "origem": "rota_conhecida"}
        if not open_page(page, url, u, f"{kind}_individuais_{cid}", 700):
            continue
        _wait_ajax_route(page, f"{kind}_{cid}")
        # A aba Cursos é um shell JS em alguns contratos. O próprio CGD
        # disponibiliza o modal de demonstrativo como rota renderizada; usamos
        # essa rota somente quando a aba principal ainda está vazia.
        if kind == "disciplinas" and "carregando..." in low(body(page)):
            modal_url = f"{CGD_URL.rstrip('/')}/contratos/cursos/modal-demonstrativo-cursos/{cid}"
            if open_page(page, modal_url, u, f"cursos_modal_{cid}", 500):
                _wait_ajax_route(page, f"cursos_modal_{cid}")
                url = modal_url
        visited.add(url.split("#", 1)[0])
        snapshot = {
            "texto": kind,
            "url": page.url,
            "rota": kind,
            "origem": "rota_conhecida",
            "tabelas": [{"cabecalhos": h, "linhas": r} for h, r in table_data(page)],
            "campos_dom": _extract_dom_fields(page),
            "texto_corpo": body(page)[:60000],
        }
        route_snapshots.append(snapshot)
        if kind == "disciplinas":
            course_text = snapshot["texto_corpo"][:30000]
            rows += extract_disciplines(page, url)
            name = name or extract_name(page)
        elif kind == "horarios":
            schedule_text = snapshot["texto_corpo"][:30000]
            st = schedule_text[:20000]
            name = name or extract_name(page)
        elif kind == "frequencia":
            freq = extract_frequency(page, cid)
            name = name or extract_name(page)

    # Depois das rotas conhecidas, visita as demais rotas que o contrato
    # realmente apresentou. Isso permite descobrir novos botões/abas do CGD
    # sem hard-code e sem sair do escopo do contrato.
    for idx, route in enumerate(routes, 1):
        normalized = route["url"].split("#", 1)[0]
        if normalized in visited:
            continue
        # O certificado/impressão fica disponível como link para a aplicação,
        # mas não deve ser aberto no scraping: pode gerar PDF/download e não
        # agrega dados acadêmicos ao universo do aluno.
        if route.get("rota") == "imprimir_certificado":
            print(f"[{u}] ROTA_SOMENTE_LINK cid={cid} tipo=imprimir_certificado url={normalized}", flush=True)
            visited.add(normalized)
            continue
        if not _safe_contract_route(route, cid, sid):
            print(f"[{u}] ROTA_GLOBAL_IGNORADA cid={cid} url={normalized}", flush=True)
            continue
        snapshot = _capture_route_snapshot(page, u, cid, route, idx, sid)
        if snapshot:
            route_snapshots.append(snapshot)
            visited.add(normalized)

            # Se uma aba foi descoberta com outro URL, aproveitamos seus dados
            # para reforçar a captura das três áreas sem duplicar registros.
            if route["rota"] == "disciplinas":
                before = len(rows)
                rows += extract_disciplines(page, normalized)
                if len(rows) != before:
                    course_text = snapshot["texto_corpo"][:30000]
            elif route["rota"] == "horarios":
                if not st:
                    st = snapshot["texto_corpo"][:20000]
                    schedule_text = snapshot["texto_corpo"][:30000]
            elif route["rota"] == "frequencia":
                extra = extract_frequency(page, cid)
                if extra["registros"]:
                    freq = extra

    # Localiza o aluno relacionado ao contrato depois que as rotas já foram
    # visitadas, mantendo a mesma sessão autenticada.
    if not sid:
        html_content = page.content()
        m = re.search(r"/alunos/(\d+)", html_content, re.I)
        sid = m.group(1) if m else None

    at = ""
    if sid and open_page(page, f"{CGD_URL.rstrip('/')}/alunos/{sid}/edit", u, f"aluno_{sid}", 500):
        name = extract_name(page, name)
        at = body(page)[:25000]

    if not name:
        name = extract_name_from_sources(page, schedule_text, course_text, ctext)
    if page_is_blocked(page) or any(flag for flag in (
        "Sorry, you have been blocked" in ctext,
        "You are unable to access" in ctext,
    )):
        raise RuntimeError(f"[{u}] DETALHE_INVALIDO_CLOUDFLARE cid={cid}")

    rows, done, cur, fut = classify(rows)
    domain = _extrair_campos_dominio_browser(
        page, ctext, course_text, schedule_text, at, route_snapshots,
        response_payloads=response_payloads, cid=cid, sid=sid
    )

    def num(r, k):
        m = re.search(r"\d+", str(r.get(k) or ""))
        return int(m.group()) if m else -1

    point = max(cur, key=lambda r: (num(r, "modulo"), num(r, "passo"), num(r, "progresso"))) if cur else None

    # Evidência de captura: não confundimos "rota acessível" com "dados
    # realmente extraídos". A validação posterior decide se o detalhe pode
    # ser considerado completo.
    rota_status = {
        "contrato": bool(ctext),
        "disciplinas": any(r.get("rota") == "disciplinas" and (r.get("tabelas") or r.get("texto_corpo")) for r in route_snapshots),
        "horarios": any(r.get("rota") == "horarios" and (r.get("tabelas") or r.get("texto_corpo")) for r in route_snapshots),
        "frequencia": any(r.get("rota") == "frequencia" and (r.get("tabelas") or r.get("texto_corpo")) for r in route_snapshots),
    }
    frequencia_status = "COM_FREQUENCIA_REAL" if freq["registros"] else "SEM_FREQUENCIA_A_INVESTIGAR"

    aluno = {
        "cgd_matricula_id": cid, "nome": name or f"Contrato {cid}", "contrato": cid, "email": None, "telefone": None,
        "curso": domain.get("curso"), "turma": domain.get("turma"), "professor": domain.get("professor"),
        "status_matricula": domain.get("status_matricula"),
        "data_matricula": domain.get("data_matricula"), "data_inicio": domain.get("data_inicio"), "data_fim": domain.get("data_fim"),
        "unidade": u, "faltas": freq["faltas"], "presencas": freq["presencas"], "ultimo_acesso": None,
        "criticidade": None, "dias_desde_ultimo_acesso": None, "status": "ATIVO", "cgd_url": cu,
        "disciplinas": rows, "disciplinas_concluidas": done, "disciplinas_em_andamento": cur, "disciplinas_futuras": fut,
        "progresso_atual": point, "horarios": st, "aluno_raw": at, "frequencia_raw": freq["registros"],
        "frequencia_status": frequencia_status,
        "rotas_cgd": route_snapshots,
        "rotas_cgd_descobertas": routes,
        "rotas_cgd_status": rota_status,
        "detalhamento_completo": bool(all(rota_status.values())),
        "reposicoes": [r for r in reps if belongs(r, cid, sid, name)],
        "capturado_em": datetime.utcnow().isoformat() + "Z"
    }
    return aluno

def validate_real_detail(aluno, cid, u):
    if not aluno:
        raise RuntimeError(f"[{u}] CONTRATO_SEM_RESULTADO cid={cid}")
    nome = norm(aluno.get("nome"))
    if not nome or nome == f"Contrato {cid}" or nome == f"Aluno Contrato {cid}":
        raise RuntimeError(f"[{u}] NOME_REAL_NAO_IDENTIFICADO cid={cid}")
    status = str(aluno.get("frequencia_status") or "").strip()
    if status not in ("COM_FREQUENCIA_REAL", "SEM_FREQUENCIA_A_INVESTIGAR"):
        raise RuntimeError(f"[{u}] FREQUENCIA_NAO_PROCESSADA cid={cid} status={status!r}")
    routes = aluno.get("rotas_cgd_status") or {}
    required = ("contrato", "disciplinas", "horarios", "frequencia")
    missing = [k for k in required if not routes.get(k)]
    if missing:
        raise RuntimeError(f"[{u}] ROTAS_CGD_INCOMPLETAS cid={cid} ausentes={','.join(missing)}")
    if not aluno.get("detalhamento_completo"):
        raise RuntimeError(f"[{u}] DETALHAMENTO_NAO_COMPLETO cid={cid}")
    # As rotas podem estar acessíveis mesmo quando as telas JS devolveram
    # apenas o shell "Carregando...". Sem estes campos o registro não é
    # persistível no CFIS; isso força o fallback renderizado, sem inventar dados.
    obrigatorios = ("curso", "turma", "professor", "data_inicio")
    ausentes = [campo for campo in obrigatorios if not norm(aluno.get(campo))]
    if ausentes:
        raise RuntimeError(
            f"[{u}] CAMPOS_DOMINIO_NAO_CAPTURADOS cid={cid} "
            f"ausentes={','.join(ausentes)}"
        )
    return aluno


def detail_worker(args):
    u, cfg, cid, reps, storage_state, attempt = args
    # ProcessPool no Windows pode iniciar o filho com um sys.path diferente.
    # O patch de frequência real precisa ser carregado explicitamente antes
    # de contract_bundle, sem depender de sitecustomize/cwd.
    try:
        import importlib
        import sys as _sys
        _root = Path(__file__).resolve().parent
        if str(_root) not in _sys.path:
            _sys.path.insert(0, str(_root))
        importlib.import_module("frequency_runtime_patch")
    except Exception as exc:
        return {"ok": False, "cid": cid, "error": f"PATCH_FREQUENCIA_REAL_ERRO: {type(exc).__name__}: {exc}", "attempt": attempt}
    profile = EDGE_PROFILE_BASE / f"{u}_{cid}_{attempt}"
    profile.mkdir(parents=True, exist_ok=True)
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
            context = browser.new_context(storage_state=storage_state)
            page = context.new_page()
            aluno = contract_bundle(page, cid, u, reps)
            aluno = validate_real_detail(aluno, cid, u)
            context.close(); browser.close()
            return {"ok": True, "cid": cid, "aluno": aluno, "attempt": attempt}
    except Exception as e:
        return {"ok": False, "cid": cid, "error": repr(e), "attempt": attempt}


def process_details(u, cfg, contracts, reps, storage_state):
    if not contracts:
        return []
    workers = min(DETAIL_WORKERS, len(contracts))
    print(f"[{u}] INICIO DETALHAMENTO PARALELO: {len(contracts)} contratos / {workers} workers")
    pending = list(contracts)
    results = []
    for round_no in (1, 2):
        if not pending:
            break
        print(f"[{u}] LOTE DE DETALHAMENTO {round_no}: {len(pending)} contratos")
        args = [(u, cfg, contract_id(c), reps, storage_state, round_no) for c in pending]
        pending_next = []
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(detail_worker, a) for a in args]
            for idx, fut in enumerate(as_completed(futures), 1):
                try:
                    r = fut.result()
                except Exception as e:
                    r = {"ok": False, "cid": "desconhecido", "error": str(e), "attempt": round_no}
                if r.get("ok") and r.get("aluno"):
                    results.append(r["aluno"])
                    aluno = r["aluno"]
                    print(f"[{u}] CONTRATO_OK cid={r.get('cid')} nome={aluno.get('nome')} faltas={aluno.get('faltas')} presencas={aluno.get('presencas')} freq_registros={len(aluno.get('frequencia_raw') or [])}")
                else:
                    cid = r.get("cid")
                    if cid and cid != "desconhecido":
                        pending_next.append(cid)
                    print(f"[{u}] FALHA DETALHE {r.get('cid')}: {r.get('error')}")
                if idx % max(1, workers) == 0 or idx == len(futures):
                    print(f"[{u}] PROGRESSO DETALHAMENTO: {idx}/{len(futures)} sucesso_total={len(results)} falhas_rodada={len(pending_next)}")
        pending = [contract_url(cid) for cid in pending_next if cid]
    print(f"[{u}] DETALHAMENTO FINALIZADO: sucesso={len(results)} falhas={len(pending)} de={len(contracts)}")
    for contract in pending:
        print(f"[{u}] CONTRATO_NAO_CAPTURADO: {contract}")
    return results


def get_replacements(page, u):
    for _, h in links(page):
        if "reposi" in low(h):
            if open_page(page, h, u, "reposicoes"):
                return extract_replacements(page, u)
    for url in (f"{CGD_URL.rstrip('/')}/individuais/reposicao", f"{CGD_URL.rstrip('/')}/reposicoes"):
        if open_page(page, url, u, "reposicoes_direta"):
            out = extract_replacements(page, u)
            if out:
                return out
    return []


def run_unit(u, cfg, pw):
    try:
        import importlib
        importlib.import_module("frequency_runtime_patch")
    except Exception as exc:
        raise RuntimeError(f"PATCH_FREQUENCIA_REAL_ERRO: {type(exc).__name__}: {exc}") from exc
    profile = EDGE_PROFILE_BASE / u
    profile.mkdir(parents=True, exist_ok=True)
    browser = pw.chromium.launch(channel="msedge", headless=HEADLESS)
    context = browser.new_context()
    page = context.new_page()
    try:
        login(page, cfg["usuario"], cfg["senha"], u)
        contracts = discover_contracts(page, u, cfg["destino"])
        reps = get_replacements(page, u)
        print(f"[{u}] REPOSICOES GLOBAIS CAPTURADAS: {len(reps)}")
        state = profile / "storage_state.json"
        context.storage_state(path=str(state))
    except Exception as e:
        print(f"[{u}] ERRO FATAL: {e}")
        return []
    finally:
        context.close(); browser.close()
    return process_details(u, cfg, contracts, reps, str(state))


def main():
    print("=" * 80)
    print("SCRAPER CGD - COLETA REAL COMPLETA POR UNIDADE / ALUNO")
    print("Fluxo: autenticacao real -> listagem real -> reposicoes -> detalhamento paralelo")
    print(f"Configuracao: workers={DETAIL_WORKERS}, page_wait_ms={PAGE_WAIT_MS}, timeout_ms={PAGE_TIMEOUT_MS}, diagnostico={DIAGNOSTICO}")
    print("=" * 80)
    all_alunos = []
    with sync_playwright() as pw:
        for u in ("matriz", "filial"):
            all_alunos += run_unit(u, CONFIG[u], pw)
    try:
        JSON_PATH.write_text(json.dumps(all_alunos, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception as e:
        print(f"ERRO gravando {JSON_PATH}: {e}")
    print("=" * 80)
    print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(all_alunos)}")
    print(f"MATRIZ: {sum(1 for a in all_alunos if a.get('unidade') == 'matriz')}")
    print(f"FILIAL: {sum(1 for a in all_alunos if a.get('unidade') == 'filial')}")
    print("=" * 80)
    if not all_alunos:
        print("Nenhum aluno foi capturado pelo CGD.")


if __name__ == "__main__":
    main()