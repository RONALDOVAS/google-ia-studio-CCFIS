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

# Edge instalado no Windows do runner, com perfil persistente exclusivo do scraper.
EDGE_EXECUTABLE = os.getenv("EDGE_EXECUTABLE") or r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"
EDGE_PROFILE_DIR = Path(os.getenv("EDGE_PROFILE_DIR") or "edge_cgd_profile")


def salvar_diagnostico(page, unidade_nome, etapa):
    try:
        prefixo = unidade_nome.lower().replace(" ", "_")
        etapa_limpa = etapa.lower().replace(" ", "_").replace("/", "_")
        screenshot_path = DIAGNOSTICO_DIR / f"{prefixo}_{etapa_limpa}.png"
        html_path = DIAGNOSTICO_DIR / f"{prefixo}_{etapa_limpa}.html"
        info_path = DIAGNOSTICO_DIR / f"{prefixo}_{etapa_limpa}.txt"
        page.screenshot(path=str(screenshot_path), full_page=True)
        with open(html_path, "w", encoding="utf-8") as arquivo:
            arquivo.write(page.content())
        with open(info_path, "w", encoding="utf-8") as arquivo:
            arquivo.write(f"UNIDADE: {unidade_nome}\nETAPA: {etapa}\nURL ATUAL: {page.url}\nTÍTULO: {page.title()}\n")
            arquivo.write(f"IFRAMES: {len(page.frames)}\nINPUTS: {page.locator('input').count()}\nBUTTONS: {page.locator('button').count()}\nLINKS: {page.locator('a').count()}\n")
            arquivo.write("\nTEXTOS DOS INPUTS:\n")
            for i, el in enumerate(page.locator("input").all()):
                try:
                    arquivo.write(f"[{i}] type={el.get_attribute('type')} name={el.get_attribute('name')} id={el.get_attribute('id')} placeholder={el.get_attribute('placeholder')}\n")
                except Exception:
                    pass
            arquivo.write("\nFRAMES:\n")
            for i, frame in enumerate(page.frames):
                try:
                    arquivo.write(f"[{i}] URL={frame.url}\n")
                except Exception:
                    pass
        print(f"[DIAGNÓSTICO] Arquivos salvos: {screenshot_path}, {html_path}, {info_path}")
    except Exception as erro:
        print(f"[DIAGNÓSTICO] Erro ao salvar diagnóstico de {unidade_nome}/{etapa}: {erro}")


def diagnosticar_navegacao_cgd(page, unidade_nome):
    print("=" * 80)
    print(f"[{unidade_nome}] ETAPA 3A - DIAGNÓSTICO DA NAVEGAÇÃO DO CGD")
    print("=" * 80)
    path = DIAGNOSTICO_DIR / f"{unidade_nome.lower()}_navegacao_cgd.txt"
    palavras = ["falta", "faltas", "frequencia", "frequência", "presenca", "presença", "aula", "aulas", "aluno", "alunos", "relatorio", "relatório", "chamada", "presente", "ausente", "frequências"]
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(f"UNIDADE: {unidade_nome}\nURL ATUAL: {page.url}\nTÍTULO: {page.title()}\n\n")
            try: f.write(page.locator("body").inner_text(timeout=10000)[:30000] + "\n\n")
            except Exception as e: f.write(f"Erro body: {e}\n")
            for tag in ["a", "button", "[role='menuitem']", "[role='button']"]:
                els = page.locator(tag)
                for i in range(min(els.count(), 2000)):
                    try:
                        el = els.nth(i)
                        if not el.is_visible(): continue
                        text = " ".join(el.inner_text(timeout=1000).split())
                        attrs = " ".join(str(el.get_attribute(x) or "") for x in ["href", "title", "aria-label"])
                        if any(p in (text + " " + attrs).lower() for p in palavras): f.write(f"{tag}[{i}] {text!r} {attrs!r}\n")
                    except Exception: pass
        print(f"[{unidade_nome}] DIAGNÓSTICO 3A concluído: {path}")
    except Exception as erro: print(f"[{unidade_nome}] Erro diagnóstico navegação: {erro}")


def imprimir_estado_pagina(page, unidade_nome, etapa):
    print("=" * 80)
    print(f"DIAGNÓSTICO DA PÁGINA - {unidade_nome} / {etapa}")
    print(f"URL atual: {page.url}")
    try: print(f"Título: {page.title()}")
    except Exception: pass
    try: print(f"Inputs: {page.locator('input').count()} | Botões: {page.locator('button').count()} | Links: {page.locator('a').count()} | Frames: {len(page.frames)}")
    except Exception: pass
    try: print(page.locator("body").inner_text(timeout=10000)[:5000])
    except Exception as erro: print(f"Não foi possível obter body: {erro}")
    print("=" * 80)


def verificar_elementos_login(page):
    login_selectors = ['input[type="text"]','input[type="email"]','input[name*="user" i]','input[name*="login" i]','input[name*="email" i]','input[id*="user" i]','input[id*="login" i]','input[id*="email" i]','input[placeholder*="usuário" i]','input[placeholder*="usuario" i]','input[placeholder*="login" i]','input[placeholder*="email" i]']
    pass_selectors = ['input[type="password"]','input[name*="senha" i]','input[name*="password" i]','input[id*="senha" i]','input[id*="password" i]']
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
    selectors = ['button[type="submit"]','input[type="submit"]','button:has-text("Entrar")','button:has-text("Acessar")','button:has-text("Login")','button:has-text("Logar")','button:has-text("Continuar")','input[value*="Entrar" i]','input[value*="Acessar" i]','input[value*="Login" i]']
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
    except Exception: return "normal", 0


def fazer_login_e_extrair(page, usuario, senha, url_destino, unidade_nome):
    print("#" * 80)
    print(f"INICIANDO PROCESSAMENTO: {unidade_nome}")
    alunos_capturados = []
    try:
        page.goto(CGD_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        imprimir_estado_pagina(page, unidade_nome, "apos_acessar_cgd")
        salvar_diagnostico(page, unidade_nome, "apos_acessar_cgd")
        login_input, senha_input = verificar_elementos_login(page)
        if login_input and senha_input:
            login_input.fill(usuario); senha_input.fill(senha)
            botao = localizar_botao_login(page)
            if not botao:
                salvar_diagnostico(page, unidade_nome, "botao_login_nao_encontrado")
                return alunos_capturados
            botao.click()
            try: page.wait_for_load_state("networkidle", timeout=60000)
            except Exception: pass
            page.wait_for_timeout(5000)
            imprimir_estado_pagina(page, unidade_nome, "apos_login")
            salvar_diagnostico(page, unidade_nome, "apos_login")
            diagnosticar_navegacao_cgd(page, unidade_nome)
        else:
            print(f"[{unidade_nome}] ERRO: campos de login não encontrados.")
            salvar_diagnostico(page, unidade_nome, "login_nao_encontrado")
            diagnosticar_navegacao_cgd(page, unidade_nome)
        if not url_destino: return alunos_capturados
        page.goto(url_destino, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        imprimir_estado_pagina(page, unidade_nome, "pagina_alunos")
        salvar_diagnostico(page, unidade_nome, "pagina_alunos")
        tables = page.locator("table")
        table_count = tables.count()
        line_count = page.locator("table tbody tr").count()
        print(f"[{unidade_nome}] Tabelas: {table_count} | Linhas: {line_count}")
        if table_count == 0: return alunos_capturados
        try:
            select_limit = page.locator('select[name*="length" i],select[name*="limit" i],select[name*="per_page" i]').first
            if select_limit.is_visible(timeout=3000):
                try: select_limit.select_option(value="-1"); page.wait_for_timeout(3000)
                except Exception: pass
        except Exception: pass
        pagina = 1
        while True:
            page.wait_for_timeout(2000)
            rows = page.locator("table tbody tr").all()
            for indice, row in enumerate(rows):
                try:
                    cols = [c.strip() for c in row.locator("td").all_text_contents()]
                    if len(cols) < 3: continue
                    contrato, nome, curso = cols[0], cols[1], cols[2]
                    status = cols[3].upper() if len(cols) > 3 else "ATIVO"
                    if any(x in status for x in ["DESATIVADO","ENCERRADO","INATIVO"]): continue
                    if "informática" not in curso.lower() and "informatica" not in curso.lower(): continue
                    data_inicio_str = cols[4] if len(cols) > 4 else ""
                    criticidade, dias_ativos = calcular_criticidade_e_dias(data_inicio_str)
                    unidade_db = "matriz" if unidade_nome.lower() == "matriz" else "filial"
                    if criticidade.lower() == "critico": tratativa = "aulao"
                    elif criticidade.lower() == "moderado": tratativa = "atividade_pratica"
                    elif criticidade.lower() == "atencao": tratativa = "acompanhamento"
                    else: tratativa = "normal"
                    try: data_inicio_db = datetime.strptime(data_inicio_str, "%d/%m/%Y").strftime("%Y-%m-%d") if data_inicio_str else datetime.now().strftime("%Y-%m-%d")
                    except Exception: data_inicio_db = datetime.now().strftime("%Y-%m-%d")
                    aluno_data = {
                        "cgd_matricula_id": contrato, "nome": nome, "contrato": contrato,
                        "email": None, "telefone": None, "curso": curso, "turma_nome": "", "professor_nome": "",
                        "data_inicio": data_inicio_db, "meses_contrato_total": 12, "ultima_aula": None, "ultimo_acesso": None,
                        "faltas_totais": 0, "faltas_mes_atual": 0,
                        "mes_referencia_faltas": datetime.now().strftime("%Y-%m"),
                        "criticidade": criticidade_db if (criticidade_db := criticidade.lower()) else "normal",
                        "dias_ativos": dias_ativos, "tratativa_sugerida": tratativa, "unidade": unidade_db
                    }
                    alunos_capturados.append(aluno_data)
                except Exception as erro: print(f"[{unidade_nome}] Erro linha {indice + 1}: {erro}")
            next_selectors = ['a[aria-label*="Next" i]','a[title*="Next" i]','button[aria-label*="Next" i]','li.next:not(.disabled) a','a:has-text("Próxima")','a:has-text("Next")']
            avancou = False
            for selector in next_selectors:
                try:
                    nxt = page.locator(selector).first
                    if nxt.is_visible(timeout=1000): nxt.click(); page.wait_for_timeout(2000); pagina += 1; avancou = True; break
                except Exception: pass
            if not avancou: break
        print(f"[{unidade_nome}] Alunos capturados: {len(alunos_capturados)}")
    except Exception as erro:
        print(f"[{unidade_nome}] ERRO GERAL: {erro}")
    return alunos_capturados


def main():
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("ERRO: variáveis SUPABASE_URL/SUPABASE_SERVICE_ROLE_KEY ausentes.")
        raise SystemExit(1)
    print("Iniciando Microsoft Edge instalado...")
    print(f"Executável Edge: {EDGE_EXECUTABLE}")
    print(f"Perfil persistente: {EDGE_PROFILE_DIR.resolve()}")
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(EDGE_PROFILE_DIR),
            executable_path=EDGE_EXECUTABLE,
            headless=False,
            viewport={"width": 1440, "height": 1000},
            args=["--disable-blink-features=AutomationControlled"],
        )
        try:
            page = context.pages[0] if context.pages else context.new_page()
            alunos = []
            for unidade, usuario, senha, url_destino in [
                ("Matriz", LOGIN_MATRIZ, SENHA_MATRIZ, URL_MATRIZ),
                ("Filial", LOGIN_FILIAL, SENHA_FILIAL, URL_FILIAL),
            ]:
                if not usuario or not senha:
                    print(f"[{unidade}] credenciais ausentes; ignorada")
                    continue
                if len(alunos) > 0: page = context.new_page()
                alunos.extend(fazer_login_e_extrair(page, usuario, senha, url_destino, unidade))
            JSON_PATH.write_text(json.dumps(alunos, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"Total de alunos capturados: {len(alunos)}")
            if not alunos:
                print("Falha: scraper terminou sem alunos. A execução NÃO será considerada sucesso.")
                raise SystemExit(2)
        finally:
            context.close()

if __name__ == "__main__":
    main()
