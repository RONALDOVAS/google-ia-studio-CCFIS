import os
import json
import re
import time
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
DIAGNOSTICO_DIR = Path("diagnostico_scraping")
DIAGNOSTICO_DIR.mkdir(parents=True, exist_ok=True)
JSON_PATH = Path("dados_alunos.json")
EDGE_PROFILE_BASE = Path(os.getenv("EDGE_PROFILE_DIR") or "edge_cgd_profiles")
MAX_DISCOVERY_PAGES = int(os.getenv("CGD_MAX_DISCOVERY_PAGES", "30"))


def normalizar_texto(v):
    return " ".join((v or "").replace("\xa0", " ").split())


def salvar_diagnostico(page, unidade, etapa):
    try:
        prefixo = unidade.lower()
        etapa = re.sub(r"[^a-zA-Z0-9_-]+", "_", etapa.lower()).strip("_")
        page.screenshot(path=str(DIAGNOSTICO_DIR / f"{prefixo}_{etapa}.png"), full_page=True)
        (DIAGNOSTICO_DIR / f"{prefixo}_{etapa}.html").write_text(page.content(), encoding="utf-8")
        try:
            body = page.locator("body").inner_text(timeout=5000)
        except Exception:
            body = ""
        info = [
            f"UNIDADE: {unidade}", f"ETAPA: {etapa}", f"URL: {page.url}",
            f"TITULO: {page.title()}",
            f"INPUTS: {page.locator('input').count()}",
            f"BUTTONS: {page.locator('button').count()}",
            f"LINKS: {page.locator('a').count()}", "", body[:30000]
        ]
        (DIAGNOSTICO_DIR / f"{prefixo}_{etapa}.txt").write_text("\n".join(info), encoding="utf-8")
    except Exception as e:
        print(f"[DIAGNOSTICO] {unidade}/{etapa}: {e}")


def imprimir_estado(page, unidade, etapa):
    print("=" * 80)
    print(f"[{unidade}] {etapa}")
    print(f"URL: {page.url}")
    try:
        print(f"Titulo: {page.title()}")
        print(f"Inputs={page.locator('input').count()} Buttons={page.locator('button').count()} Links={page.locator('a').count()} Frames={len(page.frames)}")
        print(normalizar_texto(page.locator("body").inner_text(timeout=5000))[:4000])
    except Exception as e:
        print(f"body indisponivel: {e}")
    print("=" * 80)


def verificar_login(page):
    users = [
        'input[type="text"]','input[type="email"]','input[name*="user" i]',
        'input[name*="login" i]','input[name*="email" i]','input[id*="user" i]',
        'input[id*="login" i]','input[id*="email" i]','input[placeholder*="usuário" i]',
        'input[placeholder*="usuario" i]','input[placeholder*="login" i]','input[placeholder*="email" i]'
    ]
    passwords = ['input[type="password"]','input[name*="senha" i]','input[name*="password" i]','input[id*="senha" i]','input[id*="password" i]']
    def first_visible(selectors):
        for s in selectors:
            try:
                loc = page.locator(s)
                for i in range(loc.count()):
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
            except Exception:
                pass
        return None
    return first_visible(users), first_visible(passwords)


def botao_login(page):
    selectors = ['button[type="submit"]','input[type="submit"]','button:has-text("Entrar")','button:has-text("Acessar")','button:has-text("Login")','button:has-text("Logar")','button:has-text("Continuar")']
    for s in selectors:
        try:
            loc = page.locator(s)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:
            pass
    return None


def calcular_criticidade(data_inicio):
    if not data_inicio:
        return "normal", 0
    for fmt in ("%d/%m/%Y", "%Y-%m-%d"):
        try:
            dt = datetime.strptime(data_inicio.strip(), fmt)
            dias = max(0, (datetime.now() - dt).days)
            return ("critico" if dias >= 90 else "moderado" if dias >= 60 else "atencao" if dias >= 30 else "normal"), dias
        except ValueError:
            continue
    return "normal", 0


def url_mesmo_host(url, base):
    try:
        return urlparse(url).netloc == urlparse(base).netloc
    except Exception:
        return False


def links_da_pagina(page, base):
    out = set()
    try:
        links = page.locator("a")
        for i in range(min(links.count(), 3000)):
            try:
                href = links.nth(i).get_attribute("href") or ""
                if not href or href.startswith(("#", "javascript:", "mailto:")):
                    continue
                absolute = urljoin(page.url, href)
                if url_mesmo_host(absolute, base):
                    out.add(absolute.split("#", 1)[0])
            except Exception:
                pass
    except Exception:
        pass
    return out


def eh_frequencia(url):
    return "/contratos/frequencias/" in urlparse(url).path.lower()


def eh_area_relevante(url):
    path = urlparse(url).path.lower()
    return any(x in path for x in ("/contratos", "/alunos", "/individuais", "/turmas"))


def descobrir_rotas(page, unidade, destino):
    """Descobre somente rotas reais expostas pela sessão autenticada."""
    rotas = set()
    visitadas = set()
    fila = []
    if destino and urlparse(destino).path not in ("", "/"):
        fila.append(destino)
    fila.append(page.url)
    while fila and len(visitadas) < MAX_DISCOVERY_PAGES:
        url = fila.pop(0)
        if url in visitadas or not url_mesmo_host(url, CGD_URL):
            continue
        visitadas.add(url)
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(1500)
            corpo = normalizar_texto(page.locator("body").inner_text(timeout=3000))
            if "Acesso negado" in corpo:
                print(f"[{unidade}] Rota sem permissão: {page.url}")
                continue
            links = links_da_pagina(page, CGD_URL)
            for link in links:
                if eh_frequencia(link):
                    rotas.add(link)
                if eh_area_relevante(link) and link not in visitadas and link not in fila:
                    fila.append(link)
        except Exception as e:
            print(f"[{unidade}] Falha ao visitar {url}: {e}")
    print(f"[{unidade}] Rotas de frequência descobertas: {len(rotas)}")
    return sorted(rotas)


def extrair_tabelas(page):
    tabelas = []
    for ti in range(page.locator("table").count()):
        try:
            t = page.locator("table").nth(ti)
            headers = [normalizar_texto(x) for x in t.locator("thead th").all_text_contents()]
            if not headers:
                headers = [normalizar_texto(x) for x in t.locator("tr:first-child th, tr:first-child td").all_text_contents()]
            rows = []
            trs = t.locator("tbody tr")
            for ri in range(trs.count()):
                vals = [normalizar_texto(x) for x in trs.nth(ri).locator("td").all_text_contents()]
                if vals:
                    rows.append(vals)
            tabelas.append((headers, rows))
        except Exception:
            pass
    return tabelas


def identificar_coluna(headers, nomes):
    for i, h in enumerate(headers):
        hn = normalizar_texto(h).lower()
        if any(n in hn for n in nomes):
            return i
    return None


def extrair_frequencia(page, unidade, rota):
    contrato_id = urlparse(rota).path.rstrip("/").split("/")[-1]
    texto = normalizar_texto(page.locator("body").inner_text(timeout=5000))
    tabelas = extrair_tabelas(page)
    registros = []
    faltas = 0
    presencas = 0
    for headers, rows in tabelas:
        i_status = identificar_coluna(headers, ["status", "situação", "presença", "presenca", "frequência", "frequencia"])
        i_data = identificar_coluna(headers, ["data", "dia"])
        i_aluno = identificar_coluna(headers, ["aluno", "nome", "estudante"])
        for row in rows:
            status = row[i_status].lower() if i_status is not None and i_status < len(row) else ""
            if any(x in status for x in ("falta", "ausente", "não compareceu", "nao compareceu")):
                faltas += 1
            elif any(x in status for x in ("presente", "presença", "presenca", "compareceu")):
                presencas += 1
            registros.append({
                "data": row[i_data] if i_data is not None and i_data < len(row) else None,
                "status": row[i_status] if i_status is not None and i_status < len(row) else None,
                "aluno": row[i_aluno] if i_aluno is not None and i_aluno < len(row) else None,
                "colunas": row,
                "cabecalhos": headers,
            })
    return {
        "contrato_cgd": contrato_id,
        "rota_frequencia": rota,
        "faltas_totais": faltas,
        "presencas_totais": presencas,
        "registros": registros,
        "texto_contexto": texto[:12000],
    }


def extrair_disciplinas(page, unidade, rota):
    """Extrai disciplinas, módulos, passos e datas somente quando aparecem no CGD."""
    tabelas = extrair_tabelas(page)
    itens = []
    palavras = ("disciplina", "módulo", "modulo", "passo", "etapa", "aula", "progresso", "carga horária", "carga horaria")
    for headers, rows in tabelas:
        if not any(any(p in h.lower() for p in palavras) for h in headers):
            continue
        for row in rows:
            item = {"cabecalhos": headers, "valores": row}
            for key, nomes in {
                "disciplina": ("disciplina",), "modulo": ("módulo", "modulo"),
                "passo": ("passo", "etapa"), "progresso": ("progresso",),
                "carga_horaria": ("carga horária", "carga horaria"), "data": ("data",)
            }.items():
                idx = identificar_coluna(headers, nomes)
                item[key] = row[idx] if idx is not None and idx < len(row) else None
            # Preserva status real quando o cabeçalho existir.
            idx_status = identificar_coluna(headers, ("status", "situação", "situacao", "estado"))
            item["status"] = row[idx_status] if idx_status is not None and idx_status < len(row) else None
            itens.append(item)
    return itens


def montar_aluno(contrato, unidade, dados_freq, disciplinas):
    nome = None
    curso = None
    for reg in dados_freq["registros"]:
        if reg.get("aluno"):
            nome = reg["aluno"]
            break
    texto = dados_freq.get("texto_contexto", "")
    if not nome:
        m = re.search(r"(?:Aluno|Estudante)\s*[:\-]\s*([^|\n]{3,120})", texto, re.I)
        if m:
            nome = normalizar_texto(m.group(1))
    nome = nome or f"Contrato {contrato}"
    m = re.search(r"(?:Curso)\s*[:\-]\s*([^|\n]{3,120})", texto, re.I)
    if m:
        curso = normalizar_texto(m.group(1))

    def status_item(d):
        return normalizar_texto(" ".join(str(d.get(k) or "") for k in ("status", "progresso", "valores"))).lower()

    concluidas = [d for d in disciplinas if "100%" in status_item(d) or any(x in status_item(d) for x in ("concluída", "concluida", "concluído", "concluido", "finalizada", "finalizado"))]
    futuras = [d for d in disciplinas if any(x in status_item(d) for x in ("não inici", "nao inici", "aguard", "futura"))]
    andamento = [d for d in disciplinas if any(x in status_item(d) for x in ("andamento", "em curso", "iniciada", "iniciado", "progresso")) and d not in concluidas]
    atual = andamento[0] if andamento else next((d for d in disciplinas if d.get("disciplina") or d.get("modulo") or d.get("passo")), None)

    criticidade, dias = calcular_criticidade(None)
    unidade_db = "matriz" if unidade.lower() == "matriz" else "filial"
    return {
        "cgd_matricula_id": contrato,
        "nome": nome,
        "contrato": contrato,
        "email": None,
        "telefone": None,
        "curso": curso,
        "turma_nome": None,
        "professor_nome": None,
        "data_inicio": None,
        "meses_contrato_total": None,
        "ultima_aula": None,
        "ultimo_acesso": None,
        "faltas_totais": dados_freq["faltas_totais"],
        "faltas_mes_atual": 0,
        "mes_referencia_faltas": datetime.now().strftime("%m/%Y"),
        "dias_em_curso": dias,
        "criticidade": criticidade,
        "tratativa_sugerida": "normal",
        "status_tratativa": "pendente",
        "status_matricula": "ativo",
        "bloqueado_automaticamente": dados_freq["faltas_totais"] > 3,
        "motivo_bloqueio": "mais_de_3_faltas" if dados_freq["faltas_totais"] > 3 else None,
        "total_disciplinas_grade": len(disciplinas),
        "disciplinas_concluidas": len(concluidas),
        "unidade": unidade_db,
        "disciplinas_detalhadas": disciplinas,
        "disciplinas_concluidas_detalhadas": concluidas,
        "disciplinas_em_andamento_detalhadas": andamento,
        "disciplinas_futuras_detalhadas": futuras,
        "disciplina_atual": atual,
        "modulo_atual": atual.get("modulo") if atual else None,
        "passo_atual": atual.get("passo") if atual else None,
        "data_ponto_atual": atual.get("data") if atual else None,
        "frequencia_detalhada": dados_freq["registros"],
        "origem_dados": "cgd_live",
        "rota_cgd": dados_freq["rota_frequencia"],
    }


def login_unidade(page, usuario, senha, unidade):
    page.goto(CGD_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3000)
    login, password = verificar_login(page)
    if login and password:
        login.fill(usuario)
        password.fill(senha)
        btn = botao_login(page)
        if not btn:
            raise RuntimeError("Botão de login não encontrado")
        btn.click()
        try:
            page.wait_for_load_state("networkidle", timeout=60000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
    imprimir_estado(page, unidade, "apos_login")
    salvar_diagnostico(page, unidade, "apos_login")


def processar_unidade(context, usuario, senha, destino, unidade):
    page = context.new_page()
    try:
        print(f"\n### {unidade} ###")
        login_unidade(page, usuario, senha, unidade)
        rotas = descobrir_rotas(page, unidade, destino)
        if not rotas:
            salvar_diagnostico(page, unidade, "nenhuma_rota_frequencia")
            print(f"[{unidade}] Nenhuma rota /contratos/frequencias/ foi encontrada na sessão.")
            return []
        alunos = []
        vistos = set()
        for idx, rota in enumerate(rotas, 1):
            if rota in vistos:
                continue
            vistos.add(rota)
            try:
                print(f"[{unidade}] Frequência {idx}/{len(rotas)}: {rota}")
                page.goto(rota, wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2000)
                imprimir_estado(page, unidade, f"frequencia_{idx}")
                salvar_diagnostico(page, unidade, f"frequencia_{idx}")
                freq = extrair_frequencia(page, unidade, rota)
                disciplinas = extrair_disciplinas(page, unidade, rota)
                contrato = freq["contrato_cgd"]
                aluno = montar_aluno(contrato, unidade, freq, disciplinas)
                alunos.append(aluno)
                print(f"[{unidade}] CAPTURADO contrato={contrato} faltas={freq['faltas_totais']} disciplinas={len(disciplinas)}")
            except Exception as e:
                print(f"[{unidade}] Erro na rota {rota}: {e}")
        return alunos
    finally:
        try:
            page.close()
        except Exception:
            pass


def atualizar_supabase(alunos):
    if not alunos:
        return
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError("SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY ausentes")
    supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
    colunas_alunos = {
        "cgd_matricula_id", "nome", "contrato", "email", "telefone", "curso", "turma_nome",
        "professor_nome", "data_inicio", "meses_contrato_total", "ultima_aula", "ultimo_acesso",
        "faltas_totais", "faltas_mes_atual", "mes_referencia_faltas", "dias_em_curso", "criticidade",
        "tratativa_sugerida", "status_tratativa", "status_matricula", "bloqueado_automaticamente",
        "motivo_bloqueio", "total_disciplinas_grade", "disciplinas_concluidas", "unidade"
    }
    payload = [{k: v for k, v in a.items() if k in colunas_alunos} for a in alunos]
    supabase.table("alunos").upsert(payload, on_conflict="cgd_matricula_id").execute()
    for unidade in ("matriz", "filial"):
        lista = [a for a in alunos if a["unidade"] == unidade]
        resumo = {
            "unidade": unidade,
            "nome_unidade": "Matriz" if unidade == "matriz" else "Filial",
            "total_alunos_ativos": len(lista),
            "total_matriz": len(lista) if unidade == "matriz" else 0,
            "total_filial": len(lista) if unidade == "filial" else 0,
            "alunos_criticos": sum(a["criticidade"] == "critico" for a in lista),
            "alunos_moderados": sum(a["criticidade"] == "moderado" for a in lista),
            "total_contratos": len(lista),
            "laboratorios_ativos": [],
            "criticos": sum(a["criticidade"] == "critico" for a in lista),
            "moderados": sum(a["criticidade"] == "moderado" for a in lista),
            "atencao": sum(a["criticidade"] == "atencao" for a in lista),
            "normais": sum(a["criticidade"] == "normal" for a in lista),
            "bloqueados_faltas": sum(a["bloqueado_automaticamente"] for a in lista),
            "mes_referencia": datetime.now().strftime("%m/%Y"),
            "alunos_data": lista,
            "origem": "cgd_live",
            "ultimo_sync": datetime.now().isoformat(),
        }
        supabase.table("resumo_cgd").upsert(resumo, on_conflict="unidade").execute()


def main():
    required = {
        "SUPABASE_URL": SUPABASE_URL,
        "SUPABASE_SERVICE_ROLE_KEY": SUPABASE_KEY,
        "CGD_LOGIN_URL": CGD_URL,
        "CGD_USER_MATRIZ": LOGIN_MATRIZ,
        "CGD_PASS_MATRIZ": SENHA_MATRIZ,
        "CGD_USER_FILIAL": LOGIN_FILIAL,
        "CGD_PASS_FILIAL": SENHA_FILIAL,
    }
    for name, value in required.items():
        print(f"{name}: {'OK' if value else 'AUSENTE'}")
    if not all(required.values()):
        raise SystemExit(1)

    todos = []
    with sync_playwright() as p:
        for unidade, usuario, senha, destino, profile in (
            ("Matriz", LOGIN_MATRIZ, SENHA_MATRIZ, URL_MATRIZ, "matriz"),
            ("Filial", LOGIN_FILIAL, SENHA_FILIAL, URL_FILIAL, "filial"),
        ):
            if not usuario or not senha:
                continue
            profile_dir = EDGE_PROFILE_BASE / profile
            profile_dir.mkdir(parents=True, exist_ok=True)
            context = p.chromium.launch_persistent_context(
                user_data_dir=str(profile_dir),
                channel="msedge",
                headless=False,
                viewport={"width": 1440, "height": 1000},
            )
            try:
                todos.extend(processar_unidade(context, usuario, senha, destino, unidade))
            finally:
                context.close()

    unicos = {}
    for aluno in todos:
        unicos[(aluno["unidade"], aluno["contrato"])] = aluno
    todos = list(unicos.values())
    JSON_PATH.write_text(json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(todos)}")
    if not todos:
        print("Falha: nenhuma rota de frequência com dados foi capturada. Execução não é sucesso.")
        raise SystemExit(2)
    atualizar_supabase(todos)
    print("Sincronização Supabase concluída.")


if __name__ == "__main__":
    main()
