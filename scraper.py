import os
import json
import re
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse, urljoin

from playwright.sync_api import sync_playwright
from supabase import create_client, Client

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
CGD_URL = os.getenv("CGD_LOGIN_URL") or "https://app.cgd.com.br/"

CONFIG = {
    "matriz": {
        "usuario": os.getenv("CGD_USER_MATRIZ"),
        "senha": os.getenv("CGD_PASS_MATRIZ"),
        "destino": os.getenv("CGD_MATRIZ_URL"),
    },
    "filial": {
        "usuario": os.getenv("CGD_USER_FILIAL"),
        "senha": os.getenv("CGD_PASS_FILIAL"),
        "destino": os.getenv("CGD_FILIAL_URL"),
    },
}

JSON_PATH = Path("dados_alunos.json")
DIAGNOSTICO_DIR = Path("diagnostico_scraping")
DIAGNOSTICO_DIR.mkdir(parents=True, exist_ok=True)
EDGE_PROFILE_BASE = Path(os.getenv("EDGE_PROFILE_DIR") or "edge_cgd_profiles")
MAX_CONTRACTS = int(os.getenv("CGD_MAX_CONTRACTS", "500"))
MAX_LINK_PAGES = int(os.getenv("CGD_MAX_LINK_PAGES", "80"))


def norm(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def low(value):
    return norm(value).lower()


def same_host(url):
    try:
        return urlparse(url).netloc == urlparse(CGD_URL).netloc
    except Exception:
        return False


def absolute(page, href):
    if not href:
        return ""
    return urljoin(page.url, href).split("#", 1)[0]


def page_links(page):
    result = []
    try:
        anchors = page.locator("a")
        for i in range(min(anchors.count(), 5000)):
            try:
                a = anchors.nth(i)
                href = absolute(page, a.get_attribute("href") or "")
                if not href or not same_host(href):
                    continue
                result.append((norm(a.inner_text()), href))
            except Exception:
                continue
    except Exception:
        pass
    return result


def unique_urls(items):
    seen = set()
    out = []
    for item in items:
        url = item[1] if isinstance(item, tuple) else item
        if url and url not in seen:
            seen.add(url)
            out.append(item)
    return out


def dump(page, unidade, nome):
    try:
        safe = re.sub(r"[^a-zA-Z0-9_-]+", "_", nome.lower())
        prefix = f"{unidade}_{safe}"
        (DIAGNOSTICO_DIR / f"{prefix}.html").write_text(page.content(), encoding="utf-8")
        (DIAGNOSTICO_DIR / f"{prefix}.txt").write_text(
            norm(page.locator("body").inner_text())[:100000], encoding="utf-8"
        )
        page.screenshot(path=str(DIAGNOSTICO_DIR / f"{prefix}.png"), full_page=True)
    except Exception:
        pass


def open_page(page, url, unidade, nome, wait=1600):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(wait)
        print(f"[{unidade}] {nome}: {page.url}")
        dump(page, unidade, nome)
        return same_host(page.url)
    except Exception as exc:
        print(f"[{unidade}] erro abrindo {url}: {exc}")
        return False


def login(page, usuario, senha, unidade):
    page.goto(CGD_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)

    # O perfil persistente normalmente já chega autenticado. Só preenche o login
    # quando os campos realmente aparecem.
    users = page.locator(
        'input[type="text"],input[type="email"],input[name*="user" i],'
        'input[name*="login" i],input[name*="email" i]'
    )
    passwords = page.locator(
        'input[type="password"],input[name*="senha" i],input[name*="password" i]'
    )
    u = None
    p = None
    for i in range(users.count()):
        try:
            if users.nth(i).is_visible():
                u = users.nth(i)
                break
        except Exception:
            pass
    for i in range(passwords.count()):
        try:
            if passwords.nth(i).is_visible():
                p = passwords.nth(i)
                break
        except Exception:
            pass

    if u and p and usuario and senha:
        u.fill(usuario)
        p.fill(senha)
        buttons = page.locator(
            'button[type="submit"],input[type="submit"],'
            'button:has-text("Entrar"),button:has-text("Acessar"),'
            'button:has-text("Login")'
        )
        btn = None
        for i in range(buttons.count()):
            try:
                if buttons.nth(i).is_visible():
                    btn = buttons.nth(i)
                    break
            except Exception:
                pass
        if not btn:
            raise RuntimeError(f"[{unidade}] botão de login não encontrado")
        btn.click()
        page.wait_for_timeout(3500)

    print(f"[{unidade}] apos_login: {page.url}")
    dump(page, unidade, "apos_login")


def is_contract(url):
    path = urlparse(url).path.rstrip("/").lower()
    m = re.fullmatch(r"/contratos/(\d+)", path)
    return bool(m)


def contract_id(url):
    path = urlparse(url).path.rstrip("/")
    m = re.search(r"/contratos/(\d+)$", path)
    return m.group(1) if m else None


def is_frequency(url):
    return bool(re.search(r"/contratos/frequencias/\d+", urlparse(url).path, re.I))


def is_course(url):
    return bool(re.search(r"/contratos/cursos/\d+", urlparse(url).path, re.I))


def is_schedule(url):
    return bool(re.search(r"/contratos/horarios/\d+", urlparse(url).path, re.I))


def is_student(url):
    return "/alunos/" in urlparse(url).path.lower()


def is_replacement(text, url):
    s = low(text + " " + url)
    return "reposi" in s


def classify_links(page):
    ls = unique_urls(page_links(page))
    contracts = [u for t, u in ls if is_contract(u)]
    frequencies = [u for t, u in ls if is_frequency(u)]
    courses = [u for t, u in ls if is_course(u)]
    schedules = [u for t, u in ls if is_schedule(u)]
    students = [u for t, u in ls if is_student(u)]
    replacements = [(t, u) for t, u in ls if is_replacement(t, u)]
    return {
        "all": ls,
        "contracts": unique_urls(contracts),
        "frequencies": unique_urls(frequencies),
        "courses": unique_urls(courses),
        "schedules": unique_urls(schedules),
        "students": unique_urls(students),
        "replacements": unique_urls(replacements),
    }


def print_relevant_links(page, unidade):
    c = classify_links(page)
    print(
        f"[{unidade}] links reais encontrados: "
        f"contratos={len(c['contracts'])} frequencias={len(c['frequencies'])} "
        f"cursos={len(c['courses'])} horarios={len(c['schedules'])} "
        f"alunos={len(c['students'])} reposicoes={len(c['replacements'])}"
    )
    for text, url in c["all"]:
        s = low(text + " " + url)
        if any(k in s for k in (
            "contrato", "frequência", "frequencia", "curso", "horário", "horario",
            "aluno", "reposição", "reposicao", "individual"
        )):
            print(f"[{unidade}] LINK: {text!r} -> {url}")
    return c


def discover_contracts(page, unidade, destino):
    """Descobre TODOS os contratos disponíveis ao perfil, não apenas o contrato de teste."""
    found = []
    seen = set()

    # 1) A rota configurada é usada como ponto inicial quando existir.
    if destino and same_host(destino):
        open_page(page, destino, unidade, "rota_configurada")
        c = classify_links(page)
        if is_contract(page.url):
            found.append(page.url)
        found.extend(c["contracts"])
        print_relevant_links(page, unidade)

    # 2) A página inicial/menu é obrigatória para descobrir a lista real de contratos.
    open_page(page, CGD_URL, unidade, "inicio")
    c = print_relevant_links(page, unidade)
    found.extend(c["contracts"])

    # 3) Segue o link real "Contratos" do menu, se ele existir, para obter a lista.
    menu_candidates = []
    for text, url in c["all"]:
        if low(text).strip() == "contratos" or low(text).startswith("contratos"):
            menu_candidates.append(url)
    # Se o menu não tiver texto, uma rota /contratos é aceitável apenas quando
    # descoberta como href real da página.
    menu_candidates.extend(
        [u for _, u in c["all"] if urlparse(u).path.rstrip("/").lower() == "/contratos"]
    )

    for menu_url in unique_urls(menu_candidates):
        if open_page(page, menu_url, unidade, "lista_contratos", wait=1800):
            cc = print_relevant_links(page, unidade)
            found.extend(cc["contracts"])
            # Algumas listagens usam paginação; os links das páginas seguintes
            # também são seguidos, sem inventar URLs.
            for text, href in cc["all"]:
                if any(k in low(text) for k in ("próxima", "proxima", "next")):
                    if open_page(page, href, unidade, "lista_contratos_pagina", wait=1200):
                        cp = print_relevant_links(page, unidade)
                        found.extend(cp["contracts"])

    # 4) Quando o menu entrega apenas Alunos, segue os links reais de aluno e
    # recolhe os contratos que o CGD disponibilizar para aquele perfil.
    queue = []
    for text, url in page_links(page):
        if any(k in low(text + " " + url) for k in ("aluno", "contrato", "individual")):
            queue.append(url)

    visited = set()
    for url in queue[:MAX_LINK_PAGES]:
        if url in visited or not same_host(url):
            continue
        visited.add(url)
        if not open_page(page, url, unidade, f"descoberta_{len(visited)}", wait=1000):
            continue
        cc = classify_links(page)
        found.extend(cc["contracts"])
        if len(set(found)) >= MAX_CONTRACTS:
            break

    result = []
    for url in found:
        cid = contract_id(url)
        if cid and cid not in seen:
            seen.add(cid)
            result.append(f"{urlparse(url).scheme}://{urlparse(url).netloc}/contratos/{cid}")
        if len(result) >= MAX_CONTRACTS:
            break

    print(f"[{unidade}] CONTRATOS ÚNICOS DESCOBERTOS: {len(result)}")
    for url in result:
        print(f"[{unidade}] CONTRATO: {url}")
    return result


def tables(page):
    out = []
    try:
        ts = page.locator("table")
        for i in range(ts.count()):
            t = ts.nth(i)
            heads = [norm(x) for x in t.locator("thead th").all_text_contents()]
            if not heads:
                heads = [norm(x) for x in t.locator("tr:first-child th, tr:first-child td").all_text_contents()]
            rows = []
            body_rows = t.locator("tbody tr")
            if body_rows.count() == 0:
                body_rows = t.locator("tr").nth(1)
            for j in range(body_rows.count() if hasattr(body_rows, "count") else 0):
                try:
                    vals = [norm(x) for x in body_rows.nth(j).locator("td").all_text_contents()]
                    if vals:
                        rows.append(vals)
                except Exception:
                    pass
            out.append((heads, rows))
    except Exception:
        pass
    return out


def column(headers, names):
    names = tuple(low(x) for x in names)
    for i, h in enumerate(headers):
        h = low(h)
        if any(n in h for n in names):
            return i
    return None


def text_body(page):
    try:
        return norm(page.locator("body").inner_text())
    except Exception:
        return ""


def extract_student(page, fallback=None):
    body = text_body(page)
    patterns = [
        r"(?:Aluno|Estudante|Nome)\s*[:\-]\s*([^\n|]{3,150})",
    ]
    for pattern in patterns:
        m = re.search(pattern, body, re.I)
        if m:
            value = norm(m.group(1))
            if value and value.lower() not in ("aluno", "estudante", "nome"):
                return value
    return fallback


def extract_frequency(page, frequency_url):
    cid = contract_id(frequency_url)
    records = []
    faltas = 0
    presencas = 0
    for headers, rows in tables(page):
        status_i = column(headers, ("status", "situação", "situacao", "presença", "presenca", "frequência", "frequencia"))
        date_i = column(headers, ("data", "dia"))
        student_i = column(headers, ("aluno", "nome", "estudante"))
        for row in rows:
            status = low(row[status_i]) if status_i is not None and status_i < len(row) else ""
            if any(x in status for x in ("falta", "ausente", "não compareceu", "nao compareceu")):
                faltas += 1
            elif any(x in status for x in ("presente", "presença", "presenca", "compareceu")):
                presencas += 1
            records.append({
                "data": row[date_i] if date_i is not None and date_i < len(row) else None,
                "status": row[status_i] if status_i is not None and status_i < len(row) else None,
                "aluno": row[student_i] if student_i is not None and student_i < len(row) else None,
                "valores": row,
                "cabecalhos": headers,
            })
    return {
        "contrato": cid,
        "rota": frequency_url,
        "nome": extract_student(page),
        "faltas": faltas,
        "presencas": presencas,
        "registros": records,
        "contexto": text_body(page)[:20000],
    }


def extract_replacements(page, unidade):
    result = []
    for headers, rows in tables(page):
        joined = low(" ".join(headers))
        if not any(x in joined for x in ("reposição", "reposicao", "contrato", "aluno", "data")):
            continue
        for row in rows:
            result.append({"cabecalhos": headers, "valores": row, "unidade": unidade})
    return result


def extract_discipline_rows(page, source_url):
    result = []
    for headers, rows in tables(page):
        joined = low(" ".join(headers))
        if not any(x in joined for x in (
            "disciplina", "módulo", "modulo", "passo", "etapa", "progresso",
            "carga horária", "carga horaria", "conclu", "status"
        )):
            continue
        for row in rows:
            item = {
                "disciplina": None,
                "modulo": None,
                "passo": None,
                "progresso": None,
                "carga_horaria": None,
                "data": None,
                "status": None,
                "cabecalhos": headers,
                "valores": row,
                "origem": source_url,
            }
            for key, names in {
                "disciplina": ("disciplina",),
                "modulo": ("módulo", "modulo"),
                "passo": ("passo", "etapa"),
                "progresso": ("progresso",),
                "carga_horaria": ("carga horária", "carga horaria", "carga"),
                "data": ("data", "última", "ultima"),
                "status": ("status", "situação", "situacao", "estado"),
            }.items():
                idx = column(headers, names)
                if idx is not None and idx < len(row):
                    item[key] = row[idx]
            result.append(item)
    return result


def extract_discipline_from_text(page, source_url):
    body = text_body(page)
    if not body:
        return []
    # Mantém também o texto bruto porque algumas telas do CGD renderizam
    # módulo/passo como cards, e não como tabela.
    items = []
    module_matches = list(re.finditer(r"M[oó]dulo\s*(\d+)\b", body, re.I))
    for idx, match in enumerate(module_matches):
        start = max(0, match.start() - 120)
        end = module_matches[idx + 1].start() if idx + 1 < len(module_matches) else min(len(body), match.end() + 800)
        chunk = norm(body[start:end])
        step = None
        sm = re.search(r"(?:Passo|Etapa)\s*(\d+)\b", chunk, re.I)
        if sm:
            step = sm.group(1)
        progress = None
        pm = re.search(r"(\d{1,3})\s*%", chunk)
        if pm:
            progress = pm.group(1) + "%"
        date = None
        dm = re.search(r"\b(\d{1,2}/\d{1,2}/\d{2,4})\b", chunk)
        if dm:
            date = dm.group(1)
        items.append({
            "disciplina": None,
            "modulo": match.group(1),
            "passo": step,
            "progresso": progress,
            "carga_horaria": None,
            "data": date,
            "status": None,
            "texto_contexto": chunk,
            "cabecalhos": [],
            "valores": [],
            "origem": source_url,
        })
    return items


def merge_disciplines(rows):
    seen = set()
    result = []
    for item in rows:
        key = json.dumps({
            "disciplina": item.get("disciplina"),
            "modulo": item.get("modulo"),
            "passo": item.get("passo"),
            "progresso": item.get("progresso"),
            "data": item.get("data"),
            "origem": item.get("origem"),
        }, ensure_ascii=False, sort_keys=True)
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def status_text(item):
    return low(" ".join(str(item.get(k) or "") for k in (
        "status", "progresso", "texto_contexto", "valores"
    )))


def classify_disciplines(rows):
    completed = []
    current = []
    future = []
    for item in rows:
        s = status_text(item)
        progress = item.get("progresso") or ""
        if "100%" in progress or any(x in s for x in (
            "concluída", "concluida", "concluído", "concluido", "finalizada", "finalizado"
        )):
            completed.append(item)
        elif any(x in s for x in ("não inici", "nao inici", "aguardando", "futura", "não começou", "nao comecou")):
            future.append(item)
        elif any(x in s for x in ("andamento", "em curso", "iniciad", "progresso")) or item.get("modulo") or item.get("passo"):
            current.append(item)
    return completed, current, future


def extract_student_id(url):
    m = re.search(r"/alunos/(\d+)", urlparse(url).path, re.I)
    return m.group(1) if m else None


def contract_bundle(page, contrato_url, unidade, global_replacements):
    cid = contract_id(contrato_url)
    if not cid:
        return None

    print(f"[{unidade}] >>> PROCESSANDO CONTRATO {cid}")
    open_page(page, contrato_url, unidade, f"contrato_{cid}")
    contract_links = classify_links(page)

    # As quatro rotas fornecidas pelo usuário são tratadas como partes do
    # mesmo contrato: contrato, cursos individuais, horários e frequências.
    frequency_url = next((u for u in contract_links["frequencies"] if contract_id(u) == cid), None)
    course_url = next((u for u in contract_links["courses"] if contract_id(u) == cid), None)
    schedule_url = next((u for u in contract_links["schedules"] if contract_id(u) == cid), None)
    student_url = next((u for u in contract_links["students"] if extract_student_id(u)), None)

    # Reforça a descoberta a partir das próprias páginas do contrato, sem
    # fabricar IDs: só segue hrefs que o CGD entregou para este contrato.
    discipline_rows = []
    schedule_text = ""
    student_name = None

    if course_url:
        open_page(page, course_url, unidade, f"cursos_individuais_{cid}")
        discipline_rows.extend(extract_discipline_rows(page, course_url))
        discipline_rows.extend(extract_discipline_from_text(page, course_url))
        cc = classify_links(page)
        # Cursos individuais podem possuir links internos para módulos/passos.
        for _, u in cc["all"][:MAX_LINK_PAGES]:
            if u == course_url:
                continue
            if "/contratos/cursos/" in urlparse(u).path.lower() and contract_id(u) == cid:
                open_page(page, u, unidade, f"curso_detalhe_{cid}", wait=900)
                discipline_rows.extend(extract_discipline_rows(page, u))
                discipline_rows.extend(extract_discipline_from_text(page, u))

    if schedule_url:
        open_page(page, schedule_url, unidade, f"horarios_{cid}")
        schedule_text = text_body(page)[:12000]

    freq = {
        "contrato": cid,
        "rota": frequency_url,
        "nome": None,
        "faltas": 0,
        "presencas": 0,
        "registros": [],
        "contexto": "",
    }
    if frequency_url:
        open_page(page, frequency_url, unidade, f"frequencia_{cid}")
        freq = extract_frequency(page, frequency_url)
        student_name = freq.get("nome") or student_name

    if student_url:
        open_page(page, student_url, unidade, f"aluno_{extract_student_id(student_url)}")
        student_name = extract_student(page, student_name)
        student_text = text_body(page)[:15000]
    else:
        student_text = ""

    # Reposições globais são filtradas por contrato/aluno abaixo. Se a tela
    # individual tiver um link específico dentro do contrato, ele também é lido.
    replacements = []
    for text, url in contract_links["replacements"]:
        if open_page(page, url, unidade, f"reposicoes_contrato_{cid}"):
            replacements.extend(extract_replacements(page, unidade))

    rows = merge_disciplines(discipline_rows)
    completed, current, future = classify_disciplines(rows)
    current_point = current[0] if current else None

    # A última posição conhecida é a posição com maior módulo/passo/progresso,
    # mas sem inventar valor quando o CGD não o forneceu.
    def numeric(item, key):
        m = re.search(r"\d+", str(item.get(key) or ""))
        return int(m.group(0)) if m else -1

    if current:
        current_point = max(current, key=lambda x: (numeric(x, "modulo"), numeric(x, "passo"), numeric(x, "progresso")))

    aluno = {
        "cgd_matricula_id": cid,
        "cgd_aluno_id": extract_student_id(student_url),
        "nome": student_name or f"Contrato {cid}",
        "contrato": cid,
        "email": None,
        "telefone": None,
        "curso": None,
        "turma_nome": None,
        "professor_nome": None,
        "data_inicio": None,
        "meses_contrato_total": None,
        "ultima_aula": None,
        "ultimo_acesso": None,
        "faltas_totais": freq["faltas"],
        "faltas_mes_atual": 0,
        "mes_referencia_faltas": datetime.now().strftime("%m/%Y"),
        "dias_em_curso": 0,
        "criticidade": "normal",
        "tratativa_sugerida": "normal",
        "status_tratativa": "pendente",
        "status_matricula": "ativo",
        "bloqueado_automaticamente": freq["faltas"] > 3,
        "motivo_bloqueio": "mais_de_3_faltas" if freq["faltas"] > 3 else None,
        "total_disciplinas_grade": len(rows),
        "disciplinas_concluidas": len(completed),
        "unidade": unidade.lower(),
        "origem_dados": "cgd_live",
        "rota_cgd": contrato_url,
        "rota_frequencia_cgd": frequency_url,
        "rota_cursos_individuais_cgd": course_url,
        "rota_horarios_individuais_cgd": schedule_url,
        "rota_aluno_cgd": student_url,
        "frequencia_detalhada": freq["registros"],
        "reposicoes_detalhadas": replacements + global_replacements,
        "disciplinas_detalhadas": rows,
        "disciplinas_concluidas_detalhadas": completed,
        "disciplinas_em_andamento_detalhadas": current,
        "disciplinas_futuras_detalhadas": future,
        "disciplina_atual": current_point.get("disciplina") if current_point else None,
        "modulo_atual": current_point.get("modulo") if current_point else None,
        "passo_atual": current_point.get("passo") if current_point else None,
        "data_ponto_atual": current_point.get("data") if current_point else None,
        "progresso_atual": current_point.get("progresso") if current_point else None,
        "carga_horaria_atual": current_point.get("carga_horaria") if current_point else None,
        "horarios_detalhados": schedule_text,
        "dados_aluno_detalhados": student_text,
    }
    print(
        f"[{unidade}] CAPTURADO: contrato={cid} aluno={aluno['nome']!r} "
        f"faltas={freq['faltas']} disciplinas={len(rows)} "
        f"concluidas={len(completed)} andamento={len(current)} futuras={len(future)}"
    )
    return aluno


def discover_global_replacements(page, unidade):
    """Localiza o link real de Individuais - Reposições e coleta a tabela global."""
    open_page(page, CGD_URL, unidade, "inicio_reposicoes")
    candidates = [(t, u) for t, u in page_links(page) if is_replacement(t, u)]
    # O nome pode aparecer exatamente como 'Individuais - Reposições' ou apenas
    # 'Reposições'. Não exigimos que a URL contenha a palavra individuais.
    candidates = unique_urls(candidates)
    for text, url in candidates:
        print(f"[{unidade}] REPOSIÇÃO REAL: {text!r} -> {url}")
    if not candidates:
        print(f"[{unidade}] Individuais - Reposições: link real não encontrado")
        return []
    text, url = candidates[0]
    if not open_page(page, url, unidade, "individuais_reposicoes"):
        return []
    rows = extract_replacements(page, unidade)
    print(f"[{unidade}] REPOSIÇÕES GLOBAIS CAPTURADAS: {len(rows)}")
    return rows


def replacement_belongs(item, aluno):
    raw = low(" ".join(str(x) for x in item.get("valores", [])))
    headers = low(" ".join(str(x) for x in item.get("cabecalhos", [])))
    cid = str(aluno.get("contrato") or "")
    aid = str(aluno.get("cgd_aluno_id") or "")
    name = low(aluno.get("nome"))
    if cid and re.search(rf"(?<!\d){re.escape(cid)}(?!\d)", raw):
        return True
    if aid and re.search(rf"(?<!\d){re.escape(aid)}(?!\d)", raw):
        return True
    if name and len(name) >= 5 and name in raw:
        return True
    # Se a tabela não possui identificador, não atribuímos a reposição a um
    # aluno por chute. Ela permanece global, preservando a informação real.
    return False


def attach_replacements(alunos, global_rows):
    for aluno in alunos:
        aluno["reposicoes_detalhadas"] = [
            r for r in global_rows if replacement_belongs(r, aluno)
        ]
    return alunos


def known_alunos_columns():
    # Mantém compatibilidade com o schema atual. Campos detalhados ficam no JSON
    # mesmo quando a tabela Supabase ainda não possui colunas equivalentes.
    return {
        "cgd_matricula_id", "nome", "contrato", "email", "telefone", "curso",
        "turma_nome", "professor_nome", "data_inicio", "meses_contrato_total",
        "ultima_aula", "ultimo_acesso", "faltas_totais", "faltas_mes_atual",
        "mes_referencia_faltas", "dias_em_curso", "criticidade",
        "tratativa_sugerida", "status_tratativa", "status_matricula",
        "bloqueado_automaticamente", "motivo_bloqueio", "total_disciplinas_grade",
        "disciplinas_concluidas", "unidade", "origem_dados", "rota_cgd",
    }


def synchronize(alunos):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("SUPABASE: não configurado; JSON será salvo normalmente.")
        return
    try:
        client: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        columns = known_alunos_columns()
        payload = []
        for aluno in alunos:
            row = {k: v for k, v in aluno.items() if k in columns}
            payload.append(row)
        if payload:
            client.table("alunos").upsert(payload, on_conflict="contrato,unidade").execute()
            print(f"SUPABASE: {len(payload)} alunos sincronizados.")
    except Exception as exc:
        print(f"SUPABASE: falha na sincronização: {exc}")


def run_unit(unidade, config, playwright):
    profile = EDGE_PROFILE_BASE / unidade
    profile.mkdir(parents=True, exist_ok=True)
    browser = playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile),
        channel="msedge",
        headless=False,
        viewport={"width": 1440, "height": 1000},
        args=["--disable-blink-features=AutomationControlled"],
    )
    page = browser.pages[0] if browser.pages else browser.new_page()
    try:
        login(page, config["usuario"], config["senha"], unidade)
        contracts = discover_contracts(page, unidade, config["destino"])
        global_replacements = discover_global_replacements(page, unidade)
        alunos = []
        for index, contrato_url in enumerate(contracts, 1):
            print(f"[{unidade}] contrato {index}/{len(contracts)}")
            try:
                aluno = contract_bundle(page, contrato_url, unidade, [])
                if aluno:
                    alunos.append(aluno)
            except Exception as exc:
                print(f"[{unidade}] erro processando {contrato_url}: {exc}")
        attach_replacements(alunos, global_replacements)
        return alunos
    finally:
        try:
            browser.close()
        except Exception:
            pass


def main():
    print("=" * 80)
    print("SCRAPER CGD — COLETA REAL POR UNIDADE / CONTRATO / ALUNO")
    print("Fluxo: contrato -> cursos individuais -> horários -> frequências -> aluno -> Individuais/Reposições")
    print("=" * 80)

    all_students = []
    with sync_playwright() as playwright:
        for unidade in ("matriz", "filial"):
            try:
                alunos = run_unit(unidade, CONFIG[unidade], playwright)
                all_students.extend(alunos)
            except Exception as exc:
                print(f"[{unidade}] ERRO FATAL DA UNIDADE: {exc}")

    # Deduplicação somente dentro da mesma unidade. Matriz e Filial jamais são misturadas.
    unique = {}
    for aluno in all_students:
        key = (low(aluno.get("unidade")), str(aluno.get("contrato") or ""))
        unique[key] = aluno
    all_students = list(unique.values())

    JSON_PATH.write_text(json.dumps(all_students, ensure_ascii=False, indent=2), encoding="utf-8")
    print("=" * 80)
    print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(all_students)}")
    print(f"MATRIZ: {sum(1 for a in all_students if a.get('unidade') == 'matriz')}")
    print(f"FILIAL: {sum(1 for a in all_students if a.get('unidade') == 'filial')}")
    print("=" * 80)

    if not all_students:
        raise SystemExit("Nenhum aluno foi capturado pelo CGD.")

    synchronize(all_students)


if __name__ == "__main__":
    main()
