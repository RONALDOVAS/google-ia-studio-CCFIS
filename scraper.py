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
LOGIN_MATRIZ = os.getenv("CGD_USER_MATRIZ")
SENHA_MATRIZ = os.getenv("CGD_PASS_MATRIZ")
URL_MATRIZ = os.getenv("CGD_MATRIZ_URL")
LOGIN_FILIAL = os.getenv("CGD_USER_FILIAL")
SENHA_FILIAL = os.getenv("CGD_PASS_FILIAL")
URL_FILIAL = os.getenv("CGD_FILIAL_URL")
CGD_URL = os.getenv("CGD_LOGIN_URL") or "https://app.cgd.com.br/"
JSON_PATH = Path("dados_alunos.json")
DIAGNOSTICO_DIR = Path("diagnostico_scraping")
DIAGNOSTICO_DIR.mkdir(parents=True, exist_ok=True)
EDGE_PROFILE_BASE = Path(os.getenv("EDGE_PROFILE_DIR") or "edge_cgd_profiles")
MAX_PAGES = int(os.getenv("CGD_MAX_DISCOVERY_PAGES", "40"))


def norm(v):
    return " ".join((v or "").replace("\xa0", " ").split())


def same_host(a, b=CGD_URL):
    try:
        return urlparse(a).netloc == urlparse(b).netloc
    except Exception:
        return False


def links(page):
    result = []
    try:
        loc = page.locator("a")
        for i in range(min(loc.count(), 3000)):
            try:
                a = loc.nth(i)
                href = a.get_attribute("href") or ""
                if not href or href.startswith(("#", "javascript:", "mailto:")):
                    continue
                href = urljoin(page.url, href).split("#", 1)[0]
                if same_host(href):
                    result.append((norm(a.inner_text()), href))
            except Exception:
                pass
    except Exception:
        pass
    return result


def dump(page, unidade, nome):
    try:
        base = f"{unidade.lower()}_{re.sub(r'[^a-zA-Z0-9_-]+', '_', nome.lower())}"
        (DIAGNOSTICO_DIR / f"{base}.html").write_text(page.content(), encoding="utf-8")
        (DIAGNOSTICO_DIR / f"{base}.txt").write_text(norm(page.locator("body").inner_text())[:50000], encoding="utf-8")
        page.screenshot(path=str(DIAGNOSTICO_DIR / f"{base}.png"), full_page=True)
    except Exception:
        pass


def login(page, usuario, senha, unidade):
    page.goto(CGD_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)
    users = page.locator('input[type="text"],input[type="email"],input[name*="user" i],input[name*="login" i],input[name*="email" i]')
    passwords = page.locator('input[type="password"],input[name*="senha" i],input[name*="password" i]')
    u = next((users.nth(i) for i in range(users.count()) if users.nth(i).is_visible()), None)
    p = next((passwords.nth(i) for i in range(passwords.count()) if passwords.nth(i).is_visible()), None)
    if u and p:
        u.fill(usuario)
        p.fill(senha)
        btns = page.locator('button[type="submit"],input[type="submit"],button:has-text("Entrar"),button:has-text("Acessar"),button:has-text("Login")')
        btn = next((btns.nth(i) for i in range(btns.count()) if btns.nth(i).is_visible()), None)
        if not btn:
            raise RuntimeError(f"[{unidade}] botão de login não encontrado")
        btn.click()
        page.wait_for_timeout(3500)
    print(f"[{unidade}] apos_login: {page.url}")
    dump(page, unidade, "apos_login")


def destino_configurado(destino):
    return bool(destino and urlparse(destino).path not in ("", "/"))


def e_frequencia(url):
    return "/contratos/frequencias/" in urlparse(url).path.lower()


def e_contrato(url):
    path = urlparse(url).path.lower()
    return "/contratos/" in path and not e_frequencia(url)


def e_reposicao(url, texto=""):
    s = (url + " " + texto).lower()
    return "reposi" in s and ("individ" in s or "/individuais" in urlparse(url).path.lower())


def coletar_links_relevantes(page, unidade):
    ls = links(page)
    freq = [u for t, u in ls if e_frequencia(u)]
    contratos = [u for t, u in ls if e_contrato(u)]
    repos = [u for t, u in ls if e_reposicao(u, t)]
    print(f"[{unidade}] links reais encontrados: contratos={len(contratos)} frequencias={len(freq)} reposicoes={len(repos)}")
    for t, u in ls:
        low = (t + " " + u).lower()
        if any(x in low for x in ("contrato", "frequência", "frequencia", "reposição", "reposicao")):
            print(f"[{unidade}] LINK: {t!r} -> {u}")
    return contratos, freq, repos


def abrir_rota(page, url, unidade, nome):
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1800)
        print(f"[{unidade}] {nome}: {page.url}")
        dump(page, unidade, nome)
        return same_host(page.url)
    except Exception as e:
        print(f"[{unidade}] erro em {url}: {e}")
        return False


def descobrir_rota_contrato(page, destino, unidade):
    """Prioriza a rota configurada. Se estiver na raiz, usa os links reais da sessão."""
    if destino_configurado(destino):
        if abrir_rota(page, destino, unidade, "rota_contrato_configurada"):
            if e_frequencia(page.url) or e_contrato(page.url):
                return page.url
    # Importante: a página 'Acesso negado' ainda entrega links de navegação.
    # Não descartamos a página antes de inspecioná-los.
    _, freq, _ = coletar_links_relevantes(page, unidade)
    if freq:
        return freq[0]

    fila = []
    vistos = set()
    for texto, href in links(page):
        low = (texto + " " + href).lower()
        if any(x in low for x in ("aluno", "contrato", "individual", "frequencia", "frequência")):
            fila.append(href)
    for _ in range(MAX_PAGES):
        if not fila:
            break
        url = fila.pop(0)
        if url in vistos or not same_host(url):
            continue
        vistos.add(url)
        if not abrir_rota(page, url, unidade, f"descoberta_{len(vistos)}"):
            continue
        contratos, freq, _ = coletar_links_relevantes(page, unidade)
        if e_frequencia(page.url):
            return page.url
        if freq:
            return freq[0]
        for u in contratos + [x[1] for x in links(page)]:
            low = u.lower()
            if same_host(u) and u not in vistos and ("/contratos/" in low or "/alunos" in low or "/individuais" in low):
                fila.append(u)
    return None


def tabelas(page):
    out = []
    ts = page.locator("table")
    for i in range(ts.count()):
        try:
            t = ts.nth(i)
            heads = [norm(x) for x in t.locator("thead th").all_text_contents()]
            if not heads:
                heads = [norm(x) for x in t.locator("tr:first-child th,tr:first-child td").all_text_contents()]
            rows = []
            trs = t.locator("tbody tr")
            for j in range(trs.count()):
                vals = [norm(x) for x in trs.nth(j).locator("td").all_text_contents()]
                if vals:
                    rows.append(vals)
            out.append((heads, rows))
        except Exception:
            pass
    return out


def col(headers, names):
    for i, h in enumerate(headers):
        h = norm(h).lower()
        if any(n in h for n in names):
            return i
    return None


def extrair_frequencia(page, rota):
    contrato = urlparse(rota).path.rstrip("/").split("/")[-1]
    registros, faltas, presencas = [], 0, 0
    for heads, rows in tabelas(page):
        si = col(heads, ("status", "situação", "situacao", "presença", "presenca", "frequência", "frequencia"))
        di = col(heads, ("data", "dia"))
        ai = col(heads, ("aluno", "nome", "estudante"))
        for row in rows:
            status = row[si].lower() if si is not None and si < len(row) else ""
            if any(x in status for x in ("falta", "ausente", "não compareceu", "nao compareceu")):
                faltas += 1
            if any(x in status for x in ("presente", "presença", "presenca", "compareceu")):
                presencas += 1
            registros.append({"data": row[di] if di is not None and di < len(row) else None,
                              "status": row[si] if si is not None and si < len(row) else None,
                              "aluno": row[ai] if ai is not None and ai < len(row) else None,
                              "valores": row, "cabecalhos": heads})
    texto = norm(page.locator("body").inner_text())
    aluno = None
    m = re.search(r"(?:Aluno|Estudante)\s*[:\-]\s*([^|\n]{3,120})", texto, re.I)
    if m:
        aluno = norm(m.group(1))
    return {"contrato": contrato, "rota": rota, "nome": aluno, "faltas": faltas,
            "presencas": presencas, "registros": registros, "contexto": texto[:15000]}


def extrair_reposicoes(page, unidade):
    repos = []
    for heads, rows in tabelas(page):
        if not any(any(x in h.lower() for x in ("reposição", "reposicao", "data", "aluno", "contrato")) for h in heads):
            continue
        for row in rows:
            repos.append({"cabecalhos": heads, "valores": row, "unidade": unidade})
    return repos


def extrair_disciplinas(page):
    itens = []
    for heads, rows in tabelas(page):
        if not any(any(x in h.lower() for x in ("disciplina", "módulo", "modulo", "passo", "etapa", "progresso", "carga horária", "carga horaria")) for h in heads):
            continue
        for row in rows:
            item = {"cabecalhos": heads, "valores": row}
            for key, names in {
                "disciplina": ("disciplina",), "modulo": ("módulo", "modulo"),
                "passo": ("passo", "etapa"), "progresso": ("progresso",),
                "carga_horaria": ("carga horária", "carga horaria"), "data": ("data",),
                "status": ("status", "situação", "situacao", "estado")}.items():
                i = col(heads, names)
                item[key] = row[i] if i is not None and i < len(row) else None
            itens.append(item)
    return itens


def buscar_reposicoes(page, unidade):
    # Usa primeiro o link real exibido pelo CGD, em vez de inventar a rota.
    candidatos = [(t, u) for t, u in links(page) if e_reposicao(u, t)]
    if not candidatos:
        # Pode estar no menu após voltar à página inicial.
        page.goto(CGD_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1200)
        candidatos = [(t, u) for t, u in links(page) if e_reposicao(u, t)]
    if not candidatos:
        print(f"[{unidade}] Link 'Individuais - Reposições' não encontrado na sessão.")
        return [], None
    texto, url = candidatos[0]
    print(f"[{unidade}] Reposições: {texto!r} -> {url}")
    abrir_rota(page, url, unidade, "individuais_reposicoes")
    return extrair_reposicoes(page, unidade), page.url


def montar_aluno(freq, disciplinas, repos, unidade):
    status_text = lambda d: norm(" ".join(str(d.get(k) or "") for k in ("status", "progresso", "valores"))).lower()
    concluidas = [d for d in disciplinas if "100%" in status_text(d) or any(x in status_text(d) for x in ("concluída", "concluida", "concluído", "concluido", "finalizada", "finalizado"))]
    futuras = [d for d in disciplinas if any(x in status_text(d) for x in ("não inici", "nao inici", "aguard", "futura"))]
    andamento = [d for d in disciplinas if d not in concluidas and any(x in status_text(d) for x in ("andamento", "em curso", "iniciad", "progresso"))]
    atual = andamento[0] if andamento else None
    nome = freq.get("nome") or next((r.get("aluno") for r in freq["registros"] if r.get("aluno")), None) or f"Contrato {freq['contrato']}"
    return {
        "cgd_matricula_id": freq["contrato"], "nome": nome, "contrato": freq["contrato"],
        "email": None, "telefone": None, "curso": None, "turma_nome": None, "professor_nome": None,
        "data_inicio": None, "meses_contrato_total": None, "ultima_aula": None, "ultimo_acesso": None,
        "faltas_totais": freq["faltas"], "faltas_mes_atual": 0,
        "mes_referencia_faltas": datetime.now().strftime("%m/%Y"), "dias_em_curso": 0,
        "criticidade": "normal", "tratativa_sugerida": "normal", "status_tratativa": "pendente",
        "status_matricula": "ativo", "bloqueado_automaticamente": freq["faltas"] > 3,
        "motivo_bloqueio": "mais_de_3_faltas" if freq["faltas"] > 3 else None,
        "total_disciplinas_grade": len(disciplinas), "disciplinas_concluidas": len(concluidas),
        "unidade": unidade.lower(), "origem_dados": "cgd_live", "rota_cgd": freq["rota"],
        "frequencia_detalhada": freq["registros"], "reposicoes_detalhadas": repos,
        "disciplinas_detalhadas": disciplinas, "disciplinas_concluidas_detalhadas": concluidas,
        "disciplinas_em_andamento_detalhadas": andamento, "disciplinas_futuras_detalhadas": futuras,
        "disciplina_atual": atual, "modulo_atual": atual.get("modulo") if atual else None,
        "passo_atual": atual.get("passo") if atual else None,
        "data_ponto_atual": atual.get("data") if atual else None,
    }


def sincronizar(alunos):
    if not alunos or not SUPABASE_URL or not SUPABASE_KEY:
        return
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    allowed = {"cgd_matricula_id","nome","contrato","email","telefone","curso","turma_nome","professor_nome",
               "data_inicio","meses_contrato_total","ultima_aula","ultimo_acesso","faltas_totais","faltas_mes_atual",
               "mes_referencia_faltas","dias_em_curso","criticidade","tratativa_sugerida","status_tratativa",
               "status_matricula","bloqueado_automaticamente","motivo_bloqueio","total_disciplinas_grade",
               "disciplinas_concluidas","unidade"}
    payload = [{k:v for k,v in a.items() if k in allowed} for a in alunos]
    supabase.table("alunos").upsert(payload, on_conflict="cgd_matricula_id").execute()


def processar(context, usuario, senha, destino, unidade):
    page = context.new_page()
    try:
        login(page, usuario, senha, unidade)
        rota = descobrir_rota_contrato(page, destino, unidade)
        if not rota:
            print(f"[{unidade}] Não encontrei nenhuma rota de contrato/frequência real.")
            return []
        if not e_frequencia(page.url):
            abrir_rota(page, rota, unidade, "frequencia_contrato")
        freq = extrair_frequencia(page, page.url)
        disciplinas = extrair_disciplinas(page)
        repos, repos_url = buscar_reposicoes(page, unidade)
        aluno = montar_aluno(freq, disciplinas, repos, unidade)
        print(f"[{unidade}] CAPTURADO: contrato={aluno['contrato']} nome={aluno['nome']} faltas={aluno['faltas_totais']} disciplinas={len(disciplinas)} reposicoes={len(repos)}")
        return [aluno]
    finally:
        page.close()


def main():
    required = {"SUPABASE_URL":SUPABASE_URL,"SUPABASE_SERVICE_ROLE_KEY":SUPABASE_KEY,
                "CGD_LOGIN_URL":CGD_URL,"CGD_USER_MATRIZ":LOGIN_MATRIZ,"CGD_PASS_MATRIZ":SENHA_MATRIZ,
                "CGD_USER_FILIAL":LOGIN_FILIAL,"CGD_PASS_FILIAL":SENHA_FILIAL}
    for k,v in required.items(): print(f"{k}: {'OK' if v else 'AUSENTE'}")
    if not all(required.values()): raise SystemExit(1)

    todos=[]
    with sync_playwright() as p:
        for unidade,usuario,senha,destino,profile in (("Matriz",LOGIN_MATRIZ,SENHA_MATRIZ,URL_MATRIZ,"matriz"),
                                                        ("Filial",LOGIN_FILIAL,SENHA_FILIAL,URL_FILIAL,"filial")):
            profile_dir=EDGE_PROFILE_BASE/profile
            profile_dir.mkdir(parents=True,exist_ok=True)
            context=p.chromium.launch_persistent_context(user_data_dir=str(profile_dir),channel="msedge",
                                                         headless=False,viewport={"width":1440,"height":1000})
            try: todos.extend(processar(context,usuario,senha,destino,unidade))
            finally: context.close()

    unicos={ (a["unidade"],a["contrato"]):a for a in todos }
    todos=list(unicos.values())
    JSON_PATH.write_text(json.dumps(todos,ensure_ascii=False,indent=2),encoding="utf-8")
    print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(todos)}")
    if not todos: raise SystemExit(1)
    sincronizar(todos)
    print("Sincronização Supabase concluída.")


if __name__ == "__main__":
    main()
