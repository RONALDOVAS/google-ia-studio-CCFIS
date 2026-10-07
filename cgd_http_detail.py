"""Detalhamento CGD por HTTP autenticado, com fallback para navegador.

O navegador autentica a sessão. Depois, os detalhes acadêmicos obrigatórios
são obtidos por HTTP e processados como HTML, sem renderizar cada rota.
"""
import re
from datetime import datetime, timezone
from urllib.parse import urlparse
import requests
from bs4 import BeautifulSoup

import scraper

CF_MARKERS = (
    "sorry, you have been blocked", "you have been blocked",
    "checking your browser", "just a moment", "challenge-platform", "cf-chl-"
)

def norm(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())

def low(value):
    return norm(value).lower()

def session_from_page(page):
    s = requests.Session()
    for c in page.context.cookies():
        s.cookies.set(c["name"], c["value"], domain=c.get("domain"), path=c.get("path", "/"))
    try:
        ua = page.evaluate("() => navigator.userAgent")
    except Exception:
        ua = None
    s.headers.update({
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "pt-BR,pt;q=0.9,en;q=0.8",
    })
    if ua:
        s.headers["User-Agent"] = ua
    return s

def _get(session, url, timeout=45):
    r = session.get(url, timeout=timeout, allow_redirects=True)
    path = urlparse(r.url).path.rstrip("/").lower()
    if "/login" == path or path.startswith("/login/"):
        raise RuntimeError(f"sessao redirecionada para login: {url}")
    r.raise_for_status()
    html = r.text
    if any(marker in html.lower() for marker in CF_MARKERS):
        raise RuntimeError(f"HTTP_CLOUDFLARE_OU_BLOQUEIO rota={url}")
    return r.url, html

def _tables(html):
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for table in soup.find_all("table"):
        heads = [norm(x.get_text(" ", strip=True)) for x in table.select("thead th")]
        if not heads:
            first = table.find("tr")
            heads = [norm(x.get_text(" ", strip=True)) for x in first.find_all(["th","td"])] if first else []
        rows = []
        tbody = table.find("tbody")
        trs = tbody.find_all("tr") if tbody else table.find_all("tr")[1:]
        for tr in trs:
            vals = [norm(x.get_text(" ", strip=True)) for x in tr.find_all("td")]
            if vals:
                rows.append(vals)
        out.append((heads, rows))
    return out

def _col(heads, *names):
    names = tuple(low(x) for x in names)
    for i,h in enumerate(heads):
        if any(n in low(h) for n in names):
            return i
    return None

def _body(html):
    return norm(BeautifulSoup(html, "html.parser").get_text(" ", strip=True))

def _name(html):
    soup = BeautifulSoup(html, "html.parser")
    for sel in ('input[name*="nome" i]', 'input[id*="nome" i]'):
        for el in soup.select(sel):
            v = norm(el.get("value"))
            if len(v) >= 3 and len(v.split()) >= 2:
                return v
    for el in soup.select("h1,h2,h3,h4,.content-header,.box-title,.card-title,.breadcrumb li"):
        cand = scraper.extract_name_from_text(norm(el.get_text(" ", strip=True)))
        if cand:
            return cand
    return scraper.extract_name_from_text(_body(html))

def _student_id(html):
    m = re.search(r"/alunos/(\d+)", html or "", re.I)
    return m.group(1) if m else None

def _label_value(text, labels):
    text = norm(text)
    for label in labels:
        m = re.search(rf"\b{re.escape(label)}\s*[:\-]\s*([^|;\n]{2,120})", text, re.I)
        if m:
            value = norm(m.group(1))
            if value:
                return value
    return None

def _first_date(text, labels):
    value = _label_value(text, labels)
    if value:
        m = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", value)
        if m:
            return m.group(0)
    return None

def _months_from_text(text):
    m = re.search(r"\b(\d{1,2})\s*mes(?:es)?\b", norm(text), re.I)
    return int(m.group(1)) if m else None

def _domain_fields(contract_text, course_text, schedule_text, aluno_text):
    sources = [contract_text, course_text, schedule_text, aluno_text]
    def first(labels):
        for source in sources:
            value = _label_value(source, labels)
            if value:
                return value
        return None
    def first_date(labels):
        for source in sources:
            value = _first_date(source, labels)
            if value:
                return value
        return None
    def first_months():
        for source in sources:
            value = _months_from_text(source)
            if value:
                return value
        return None
    return {
        "curso": first(("Curso", "Curso do aluno", "Curso contratado")),
        "turma": first(("Turma", "Turma atual", "Turma do aluno")),
        "professor": first(("Professor", "Professor responsável", "Professor responsavel")),
        "data_matricula": first_date(("Data de matrícula", "Data de matricula")),
        "data_inicio": first_date(("Data de início", "Data de inicio", "Início", "Inicio")),
        "data_fim": first_date(("Data de término", "Data de termino", "Término", "Termino", "Data fim")),
        "meses_contrato_total": first_months(),
    }

def _frequency(html):
    rec, faltas, pres = [], 0, 0
    for heads, rows in _tables(html):
        si = _col(heads, "status","situação","situacao","presença","presenca","frequência","frequencia")
        di = _col(heads, "data","dia")
        ai = _col(heads, "aluno","nome","estudante")
        for row in rows:
            s = low(row[si]) if si is not None and si < len(row) else ""
            if any(x in s for x in ("falta","ausente","não compareceu","nao compareceu")):
                faltas += 1
            elif any(x in s for x in ("presente","presença","presenca","compareceu")):
                pres += 1
            rec.append({
                "data": row[di] if di is not None and di < len(row) else None,
                "status": row[si] if si is not None and si < len(row) else None,
                "aluno": row[ai] if ai is not None and ai < len(row) else None,
                "valores": row, "cabecalhos": heads,
            })
    return {"faltas": faltas, "presencas": pres, "registros": rec}

def _disciplines(html, src):
    out = []
    for heads, rows in _tables(html):
        if not any(x in low(" ".join(heads)) for x in (
            "disciplina","módulo","modulo","passo","etapa","progresso","carga horária","carga horaria","status"
        )):
            continue
        for row in rows:
            r = {"disciplina":None,"modulo":None,"passo":None,"progresso":None,
                 "carga_horaria":None,"data":None,"status":None,
                 "cabecalhos":heads,"valores":row,"origem":src}
            for k,n in {
                "disciplina":("disciplina",),"modulo":("módulo","modulo"),
                "passo":("passo","etapa"),"progresso":("progresso",),
                "carga_horaria":("carga horária","carga horaria","carga"),
                "data":("data","última","ultima"),
                "status":("status","situação","situacao","estado")
            }.items():
                i=_col(heads,*n)
                if i is not None and i < len(row): r[k]=row[i]
            out.append(r)
    txt=_body(html)
    ms=list(re.finditer(r"M[oó]dulo\s*(\d+)\b",txt,re.I))
    for i,m in enumerate(ms):
        chunk=txt[m.start():ms[i+1].start() if i+1<len(ms) else min(len(txt),m.end()+1000)]
        sm=re.search(r"(?:Passo|Etapa)\s*(\d+)\b",chunk,re.I)
        pm=re.search(r"(\d{1,3})\s*%",chunk)
        dm=re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b",chunk)
        out.append({"disciplina":None,"modulo":m.group(1),"passo":sm.group(1) if sm else None,
                    "progresso":pm.group(1)+"%" if pm else None,
                    "carga_horaria":None,"data":dm.group(1) if dm else None,
                    "status":None,"texto_contexto":chunk[:3000],
                    "cabecalhos":[],"valores":[],"origem":src})
    return out

def _classify(rows):
    return scraper.classify(rows)

def _snapshot(kind, url, html):
    tables=[{"cabecalhos":h,"linhas":r} for h,r in _tables(html)]
    return {"texto":kind,"url":url,"rota":kind,"origem":"http_autenticado",
            "tabelas":tables,"texto_corpo":_body(html)[:60000]}

def refresh_frequency(session, cid):
    url = scraper.child_url(cid, "frequencias")
    final, html = _get(session, url)
    freq = _frequency(html)
    return freq, final, bool(_body(html) or _tables(html))

def contract_bundle_http(session, cid, unidade, reps):
    cu=scraper.contract_url(cid)
    final_contract, contract_html=_get(session,cu)
    ctext=_body(contract_html)
    name=_name(contract_html) or f"Contrato {cid}"
    sid=_student_id(contract_html)
    rows=[]; course_text=""; schedule_text=""; st=""
    freq={"faltas":0,"presencas":0,"registros":[]}
    snapshots=[]; status={"contrato":bool(ctext),"disciplinas":False,"horarios":False,"frequencia":False}

    known=(
        ("disciplinas", scraper.child_url(cid,"cursos")),
        ("horarios", scraper.child_url(cid,"horarios")),
        ("frequencia", scraper.child_url(cid,"frequencias")),
    )
    for kind,url in known:
        final,html=_get(session,url)
        snap=_snapshot(kind,final,html)
        snapshots.append(snap)
        valid=bool(snap["tabelas"] or snap["texto_corpo"])
        status[kind]=valid
        if kind=="disciplinas":
            course_text=snap["texto_corpo"][:30000]
            rows.extend(_disciplines(html,url))
            name=name if name != f"Contrato {cid}" else (_name(html) or name)
        elif kind=="horarios":
            schedule_text=snap["texto_corpo"][:30000]
            st=schedule_text[:20000]
            name=name if name != f"Contrato {cid}" else (_name(html) or name)
        else:
            freq=_frequency(html)
            name=name if name != f"Contrato {cid}" else (_name(html) or name)

    if not sid:
        sid=_student_id(" ".join(x["texto_corpo"] for x in snapshots))
    aluno_html=""
    if sid:
        final,aluno_html=_get(session,f"{scraper.CGD_URL.rstrip('/')}/alunos/{sid}/edit")
        name=_name(aluno_html) or name

    rows,done,cur,fut=_classify(rows)
    domain = _domain_fields(ctext, course_text, schedule_text, aluno_html)
    def num(r,k):
        m=re.search(r"\d+",str(r.get(k) or ""))
        return int(m.group()) if m else -1
    point=max(cur,key=lambda r:(num(r,"modulo"),num(r,"passo"),num(r,"progresso"))) if cur else None
    return {
        "cgd_matricula_id":cid,"nome":name,"contrato":cid,"email":None,"telefone":None,
        "curso":domain["curso"],"turma":domain["turma"],"professor":domain["professor"],"data_matricula":domain["data_matricula"],"data_inicio":domain["data_inicio"],
        "data_fim":domain["data_fim"],"meses_contrato_total":domain["meses_contrato_total"],"unidade":unidade,"faltas":freq["faltas"],"presencas":freq["presencas"],
        "ultimo_acesso":None,"criticidade":None,"dias_desde_ultimo_acesso":None,"status":"ATIVO",
        "cgd_url":cu,"disciplinas":rows,"disciplinas_concluidas":done,
        "disciplinas_em_andamento":cur,"disciplinas_futuras":fut,"progresso_atual":point,
        "horarios":st,"aluno_raw":aluno_html[:25000],"frequencia_raw":freq["registros"],
        "frequencia_status":"COM_FREQUENCIA_REAL" if freq["registros"] else "SEM_FREQUENCIA_A_INVESTIGAR",
        "rotas_cgd":snapshots,"rotas_cgd_descobertas":[
            {"texto":k,"url":u,"rota":k,"origem":"rota_conhecida"} for k,u in known
        ],
        "rotas_cgd_status":status,"detalhamento_completo":all(status.values()),
        "reposicoes":[r for r in reps if scraper.belongs(r,cid,sid,name)],
        "capturado_em":datetime.now(timezone.utc).isoformat(),
        "detalhamento_modo":"http_autenticado",
    }
