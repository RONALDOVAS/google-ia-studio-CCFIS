import os
import json
import time
from datetime import datetime
from pathlib import Path

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
CGD_URL = os.getenv("CGD_LOGIN_URL") or "https://cgdgestao.com.br"
DIAGNOSTICO_DIR = Path("diagnostico_scraping")
DIAGNOSTICO_DIR.mkdir(parents=True, exist_ok=True)
JSON_PATH = Path("dados_alunos.json")
EDGE_PROFILE_DIR = Path(os.getenv("EDGE_PROFILE_DIR") or "edge_cgd_profile")


def salvar_diagnostico(page, unidade_nome, etapa):
    try:
        prefixo = unidade_nome.lower().replace(" ", "_")
        etapa = etapa.lower().replace(" ", "_").replace("/", "_")
        page.screenshot(path=str(DIAGNOSTICO_DIR / f"{prefixo}_{etapa}.png"), full_page=True)
        (DIAGNOSTICO_DIR / f"{prefixo}_{etapa}.html").write_text(page.content(), encoding="utf-8")
        texto = page.locator("body").inner_text(timeout=5000) if page.locator("body").count() else ""
        info = [
            f"UNIDADE: {unidade_nome}", f"ETAPA: {etapa}", f"URL ATUAL: {page.url}",
            f"TÍTULO: {page.title()}", f"INPUTS: {page.locator('input').count()}",
            f"BUTTONS: {page.locator('button').count()}", f"LINKS: {page.locator('a').count()}",
            "", "TEXTO:", texto[:30000]
        ]
        (DIAGNOSTICO_DIR / f"{prefixo}_{etapa}.txt").write_text("\n".join(info), encoding="utf-8")
    except Exception as erro:
        print(f"[DIAGNÓSTICO] erro: {erro}")


def imprimir_estado_pagina(page, unidade_nome, etapa):
    print("=" * 80)
    print(f"[{unidade_nome}] {etapa}")
    print(f"URL: {page.url}")
    try: print(f"Título: {page.title()}")
    except Exception: pass
    try:
        print(f"Inputs={page.locator('input').count()} | Buttons={page.locator('button').count()} | Links={page.locator('a').count()} | Frames={len(page.frames)}")
        print(page.locator("body").inner_text(timeout=5000)[:5000])
    except Exception as erro: print(f"body indisponível: {erro}")
    print("=" * 80)


def pagina_bloqueada(page):
    try:
        titulo = page.title().lower()
        texto = page.locator("body").inner_text(timeout=3000).lower()
        return "attention required" in titulo or "you have been blocked" in texto or "unable to access cgd" in texto
    except Exception:
        return False


def aguardar_acesso_legitimo(page, unidade_nome, timeout_ms=180000):
    if not pagina_bloqueada(page):
        return True
    print(f"[{unidade_nome}] BLOQUEIO/VERIFICAÇÃO DETECTADO.")
    print(f"[{unidade_nome}] Edge ficará aberto para uma eventual verificação humana legítima.")
    print(f"[{unidade_nome}] Aguardando até {timeout_ms // 60000} minutos...")
    inicio = time.time()
    while (time.time() - inicio) * 1000 < timeout_ms:
        page.wait_for_timeout(5000)
        if not pagina_bloqueada(page):
            print(f"[{unidade_nome}] ACESSO CGD LIBERADO.")
            return True
    salvar_diagnostico(page, unidade_nome, "bloqueio_persistente")
    print(f"[{unidade_nome}] BLOQUEIO PERSISTENTE: não foi possível continuar sem acesso legítimo.")
    return False


def verificar_elementos_login(page):
    login_selectors = [
        'input[type="text"]','input[type="email"]','input[name*="user" i]',
        'input[name*="login" i]','input[name*="email" i]','input[id*="user" i]',
        'input[id*="login" i]','input[id*="email" i]','input[placeholder*="usuário" i]',
        'input[placeholder*="usuario" i]','input[placeholder*="login" i]','input[placeholder*="email" i]'
    ]
    pass_selectors = [
        'input[type="password"]','input[name*="senha" i]','input[name*="password" i]',
        'input[id*="senha" i]','input[id*="password" i]'
    ]
    def first_visible(selectors):
        for selector in selectors:
            try:
                loc = page.locator(selector)
                for i in range(loc.count()):
                    if loc.nth(i).is_visible(): return loc.nth(i)
            except Exception: pass
        return None
    return first_visible(login_selectors), first_visible(pass_selectors)


def localizar_botao_login(page):
    selectors = [
        'button[type="submit"]','input[type="submit"]','button:has-text("Entrar")',
        'button:has-text("Acessar")','button:has-text("Login")','button:has-text("Logar")',
        'button:has-text("Continuar")','input[value*="Entrar" i]','input[value*="Acessar" i]'
    ]
    for selector in selectors:
        try:
            loc = page.locator(selector)
            for i in range(loc.count()):
                if loc.nth(i).is_visible(): return loc.nth(i)
        except Exception: pass
    return None


def calcular_criticidade_e_dias(data_inicio_str):
    if not data_inicio_str: return "normal", 0
    try:
        data_inicio = datetime.strptime(data_inicio_str.strip(), "%d/%m/%Y")
        dias = (datetime.now() - data_inicio).days
        if dias >= 90: return "critico", dias
        if dias >= 60: return "moderado", dias
        if dias >= 30: return "atencao", dias
        return "normal", max(0, dias)
    except Exception:
        return "normal", 0


def texto_normalizado(valor):
    return " ".join((valor or "").lower().split())


def clicar_link_destino(page, url_destino, unidade_nome):
    if not url_destino:
        return False
    destino = url_destino.rstrip("/")
    destino_path = destino.split("//", 1)[-1]
    destino_path = "/" + destino_path.split("/", 1)[-1] if "/" in destino_path else ""
    candidatos = page.locator("a")
    for i in range(candidatos.count()):
        try:
            link = candidatos.nth(i)
            if not link.is_visible(): continue
            href = link.get_attribute("href") or ""
            href_abs = href.rstrip("/")
            if href_abs == destino or (destino_path and destino_path in href_abs):
                print(f"[{unidade_nome}] Clicando no link da listagem: {link.inner_text(timeout=1000).strip()!r}")
                link.click()
                page.wait_for_timeout(3000)
                return True
        except Exception: pass
    return False


def navegar_por_interface(page, url_destino, unidade_nome):
    print(f"[{unidade_nome}] Navegação pelo CGD: procurando menu/link visível, sem salto direto para a URL.")
    if clicar_link_destino(page, url_destino, unidade_nome):
        return True
    palavras = ["frequência", "frequencia", "faltas", "presença", "presenca", "contratos", "alunos", "aluno"]
    for rodada in range(3):
        links = page.locator("a")
        for i in range(links.count()):
            try:
                link = links.nth(i)
                if not link.is_visible(): continue
                texto = texto_normalizado(link.inner_text(timeout=1000))
                href = texto_normalizado(link.get_attribute("href") or "")
                if any(p in texto or p in href for p in palavras):
                    print(f"[{unidade_nome}] Clique de navegação: texto={texto!r} href={href!r}")
                    link.click()
                    page.wait_for_timeout(3000)
                    if clicar_link_destino(page, url_destino, unidade_nome): return True
                    if destino_alcançado(page, url_destino): return True
                    break
            except Exception: pass
        botoes = page.locator("button")
        for i in range(botoes.count()):
            try:
                botao = botoes.nth(i)
                if not botao.is_visible(): continue
                texto = texto_normalizado(botao.inner_text(timeout=1000) or botao.get_attribute("aria-label") or "")
                if any(p in texto for p in palavras):
                    print(f"[{unidade_nome}] Clique de menu: {texto!r}")
                    botao.click()
                    page.wait_for_timeout(1500)
                    if clicar_link_destino(page, url_destino, unidade_nome): return True
                    break
            except Exception: pass
    salvar_diagnostico(page, unidade_nome, "rota_frequencias_nao_encontrada")
    print(f"[{unidade_nome}] ERRO: não encontrei uma rota clicável até a listagem de alunos.")
    return False


def destino_alcançado(page, url_destino):
    if not url_destino: return False
    atual = page.url.rstrip("/")
    alvo = url_destino.rstrip("/")
    return atual == alvo or (alvo and alvo in atual)


def extrair_alunos_da_tabela(page, unidade_nome):
    alunos = []
    tables = page.locator("table")
    if tables.count() == 0:
        return alunos
    try:
        select_limit = page.locator('select[name*="length" i],select[name*="limit" i],select[name*="per_page" i]').first
        if select_limit.is_visible(timeout=2000):
            try: select_limit.select_option(value="-1"); page.wait_for_timeout(2000)
            except Exception: pass
    except Exception: pass
    pagina = 1
    while pagina <= 100:
        rows = page.locator("table tbody tr").all()
        print(f"[{unidade_nome}] Página {pagina}: {len(rows)} linhas.")
        for indice, row in enumerate(rows):
            try:
                cols = [c.strip() for c in row.locator("td").all_text_contents()]
                if len(cols) < 3: continue
                contrato, nome, curso = cols[0], cols[1], cols[2]
                status = cols[3].upper() if len(cols) > 3 else "ATIVO"
                if any(x in status for x in ["DESATIVADO", "ENCERRADO", "INATIVO"]): continue
                if "informática" not in curso.lower() and "informatica" not in curso.lower(): continue
                data_inicio_str = cols[4] if len(cols) > 4 else ""
                criticidade, dias_ativos = calcular_criticidade_e_dias(data_inicio_str)
                criticidade_db = criticidade.lower()
                if criticidade_db == "critico": tratativa = "aulao"
                elif criticidade_db == "moderado": tratativa = "atividade_pratica"
                elif criticidade_db == "atencao": tratativa = "acompanhamento"
                else: tratativa = "normal"
                try: data_inicio_db = datetime.strptime(data_inicio_str, "%d/%m/%Y").strftime("%Y-%m-%d") if data_inicio_str else datetime.now().strftime("%Y-%m-%d")
                except Exception: data_inicio_db = datetime.now().strftime("%Y-%m-%d")
                aluno_data = {
                    "cgd_matricula_id": contrato, "nome": nome, "contrato": contrato,
                    "email": None, "telefone": None, "curso": curso, "turma_nome": "", "professor_nome": "",
                    "data_inicio": data_inicio_db, "meses_contrato_total": 12, "ultima_aula": None, "ultimo_acesso": None,
                    "faltas_totais": 0, "faltas_mes_atual": 0,
                    "mes_referencia_faltas": datetime.now().strftime("%m/%Y"),
                    "dias_em_curso": dias_ativos, "criticidade": criticidade_db,
                    "tratativa_sugerida": tratativa, "status_tratativa": "pendente", "status_matricula": "ativo",
                    "bloqueado_automaticamente": False, "motivo_bloqueio": None,
                    "total_disciplinas_grade": 0, "disciplinas_concluidas": 0, "unidade": "matriz" if unidade_nome.lower() == "matriz" else "filial"
                }
                alunos.append(aluno_data)
                print(f"[{unidade_nome}] ALUNO CAPTURADO: {nome}")
            except Exception as erro:
                print(f"[{unidade_nome}] Erro linha {indice + 1}: {erro}")
        try:
            btn = page.locator('button:has-text("Próximo"),button:has-text("Próxima"),a:has-text("Próximo"),a:has-text("Próxima"),.paginate_button.next,[aria-label="Next"],[aria-label="Próxima"]').first
            if not btn.is_visible(timeout=1500): break
            classes = (btn.get_attribute("class") or "").lower()
            if btn.is_disabled() or "disabled" in classes or (btn.get_attribute("aria-disabled") or "").lower() == "true": break
            btn.click(); page.wait_for_timeout(2500); pagina += 1
        except Exception:
            break
    return alunos


def fazer_login_e_extrair(page, usuario, senha, url_destino, unidade_nome):
    print("#" * 80)
    print(f"INICIANDO PROCESSAMENTO: {unidade_nome}")
    try:
        page.goto(CGD_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        imprimir_estado_pagina(page, unidade_nome, "apos_acessar_cgd")
        salvar_diagnostico(page, unidade_nome, "apos_acessar_cgd")
        if not aguardar_acesso_legitimo(page, unidade_nome): return []
        login_input, senha_input = verificar_elementos_login(page)
        if login_input and senha_input:
            print(f"[{unidade_nome}] Campos de login encontrados; usando as credenciais configuradas.")
            login_input.fill(usuario)
            senha_input.fill(senha)
            botao = localizar_botao_login(page)
            if not botao:
                print(f"[{unidade_nome}] ERRO: botão de login não encontrado.")
                return []
            botao.click()
            try: page.wait_for_load_state("networkidle", timeout=60000)
            except Exception: pass
            page.wait_for_timeout(5000)
            if not aguardar_acesso_legitimo(page, unidade_nome): return []
            imprimir_estado_pagina(page, unidade_nome, "apos_login")
            salvar_diagnostico(page, unidade_nome, "apos_login")
        else:
            print(f"[{unidade_nome}] Nenhum formulário visível: mantendo a sessão já autenticada.")
        if not navegar_por_interface(page, url_destino, unidade_nome): return []
        page.wait_for_timeout(3000)
        imprimir_estado_pagina(page, unidade_nome, "pagina_alunos")
        salvar_diagnostico(page, unidade_nome, "pagina_alunos")
        alunos = extrair_alunos_da_tabela(page, unidade_nome)
        print(f"[{unidade_nome}] TOTAL DE ALUNOS VÁLIDOS: {len(alunos)}")
        salvar_diagnostico(page, unidade_nome, "final")
        return alunos
    except Exception as erro:
        print(f"[{unidade_nome}] ERRO GERAL: {erro}")
        salvar_diagnostico(page, unidade_nome, "erro_geral")
        return []


def atualizar_supabase(alunos):
    if not alunos: return
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        supabase.table("alunos").upsert(alunos, on_conflict="cgd_matricula_id").execute()
        print(f"Supabase: {len(alunos)} alunos enviados para alunos.")
        for unidade in ["matriz", "filial"]:
            lista = [a for a in alunos if a["unidade"] == unidade]
            resumo = {
                "unidade": unidade,
                "nome_unidade": "Matriz" if unidade == "matriz" else "Filial",
                "total_alunos_ativos": len(lista),
                "total_matriz": len(lista) if unidade == "matriz" else 0,
                "total_filial": len(lista) if unidade == "filial" else 0,
                "alunos_criticos": sum(a["criticidade"] == "critico" for a in lista),
                "alunos_moderados": sum(a["criticidade"] == "moderado" for a in lista),
                "total_contratos": len(lista), "laboratorios_ativos": [],
                "criticos": sum(a["criticidade"] == "critico" for a in lista),
                "moderados": sum(a["criticidade"] == "moderado" for a in lista),
                "atencao": sum(a["criticidade"] == "atencao" for a in lista),
                "normais": sum(a["criticidade"] == "normal" for a in lista),
                "bloqueados_faltas": sum(a["bloqueado_automaticamente"] for a in lista),
                "mes_referencia": datetime.now().strftime("%m/%Y"),
                "alunos_data": lista, "origem": "cgd_live", "ultimo_sync": datetime.now().isoformat()
            }
            supabase.table("resumo_cgd").upsert(resumo, on_conflict="unidade").execute()
            print(f"Supabase: resumo {unidade} atualizado.")
    except Exception as erro:
        print(f"ERRO AO ATUALIZAR SUPABASE: {erro}")


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERRO: SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY ausentes.")
        raise SystemExit(1)
    print("INÍCIO DO SCRAPER CGD")
    print(f"CGD_LOGIN_URL: {'OK' if CGD_URL else 'AUSENTE'}")
    print(f"CGD_USER_MATRIZ: {'OK' if LOGIN_MATRIZ else 'AUSENTE'}")
    print(f"CGD_PASS_MATRIZ: {'OK' if SENHA_MATRIZ else 'AUSENTE'}")
    print(f"CGD_MATRIZ_URL: {'OK' if URL_MATRIZ else 'AUSENTE'}")
    print(f"CGD_USER_FILIAL: {'OK' if LOGIN_FILIAL else 'AUSENTE'}")
    print(f"CGD_PASS_FILIAL: {'OK' if SENHA_FILIAL else 'AUSENTE'}")
    print(f"CGD_FILIAL_URL: {'OK' if URL_FILIAL else 'AUSENTE'}")
    todos = []
    with sync_playwright() as p:
        print("Iniciando Microsoft Edge instalado com perfil persistente dedicado...")
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(EDGE_PROFILE_DIR),
            channel="msedge",
            headless=False,
            viewport={"width": 1440, "height": 1000}
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            for unidade, usuario, senha, destino in [
                ("Matriz", LOGIN_MATRIZ, SENHA_MATRIZ, URL_MATRIZ),
                ("Filial", LOGIN_FILIAL, SENHA_FILIAL, URL_FILIAL),
            ]:
                if not usuario or not senha:
                    print(f"[{unidade}] credenciais ausentes; ignorada")
                    continue
                if todos:
                    page = context.new_page()
                todos.extend(fazer_login_e_extrair(page, usuario, senha, destino, unidade))
            JSON_PATH.write_text(json.dumps(todos, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"TOTAL GERAL DE ALUNOS CAPTURADOS: {len(todos)}")
            if not todos:
                print("Falha: scraper terminou sem alunos. A execução NÃO será considerada sucesso.")
                raise SystemExit(2)
            atualizar_supabase(todos)
        finally:
            context.close()


if __name__ == "__main__":
    main()
