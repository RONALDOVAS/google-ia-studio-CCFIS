import os
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

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


def salvar_diagnostico(page, unidade, etapa):
    try:
        prefixo = unidade.lower().replace(" ", "_")
        etapa = etapa.lower().replace(" ", "_").replace("/", "_")
        page.screenshot(path=str(DIAGNOSTICO_DIR / f"{prefixo}_{etapa}.png"), full_page=True)
        (DIAGNOSTICO_DIR / f"{prefixo}_{etapa}.html").write_text(page.content(), encoding="utf-8")
        try:
            texto = page.locator("body").inner_text(timeout=5000)
        except Exception:
            texto = ""
        info = [f"UNIDADE: {unidade}", f"ETAPA: {etapa}", f"URL ATUAL: {page.url}", f"TÍTULO: {page.title()}", f"INPUTS: {page.locator('input').count()}", f"BUTTONS: {page.locator('button').count()}", f"LINKS: {page.locator('a').count()}", "", "TEXTO:", texto[:30000]]
        (DIAGNOSTICO_DIR / f"{prefixo}_{etapa}.txt").write_text("\n".join(info), encoding="utf-8")
    except Exception as erro:
        print(f"[DIAGNÓSTICO] erro: {erro}")


def imprimir_estado_pagina(page, unidade, etapa):
    print("=" * 80)
    print(f"[{unidade}] {etapa}")
    print(f"URL: {page.url}")
    try:
        print(f"Título: {page.title()}")
        print(f"Inputs={page.locator('input').count()} | Buttons={page.locator('button').count()} | Links={page.locator('a').count()} | Frames={len(page.frames)}")
        print(page.locator("body").inner_text(timeout=5000)[:5000])
    except Exception as erro:
        print(f"body indisponível: {erro}")
    print("=" * 80)


def pagina_bloqueada(page):
    try:
        titulo = page.title().lower()
        texto = page.locator("body").inner_text(timeout=3000).lower()
        return "attention required" in titulo or "you have been blocked" in texto or "unable to access cgd" in texto
    except Exception:
        return False


def aguardar_acesso_legitimo(page, unidade, timeout_ms=180000):
    if not pagina_bloqueada(page):
        return True
    print(f"[{unidade}] BLOQUEIO/VERIFICAÇÃO DETECTADO; aguardando acesso legítimo no Edge.")
    inicio = time.time()
    while (time.time() - inicio) * 1000 < timeout_ms:
        page.wait_for_timeout(5000)
        if not pagina_bloqueada(page):
            print(f"[{unidade}] ACESSO CGD LIBERADO.")
            return True
    salvar_diagnostico(page, unidade, "bloqueio_persistente")
    print(f"[{unidade}] BLOQUEIO PERSISTENTE.")
    return False


def verificar_elementos_login(page):
    usuarios = ['input[type="text"]','input[type="email"]','input[name*="user" i]','input[name*="login" i]','input[name*="email" i]','input[id*="user" i]','input[id*="login" i]','input[id*="email" i]','input[placeholder*="usuário" i]','input[placeholder*="usuario" i]','input[placeholder*="login" i]','input[placeholder*="email" i]']
    senhas = ['input[type="password"]','input[name*="senha" i]','input[name*="password" i]','input[id*="senha" i]','input[id*="password" i]']
    def first_visible(selectors):
        for selector in selectors:
            try:
                loc = page.locator(selector)
                for i in range(loc.count()):
                    if loc.nth(i).is_visible():
                        return loc.nth(i)
            except Exception:
                pass
        return None
    return first_visible(usuarios), first_visible(senhas)


def localizar_botao_login(page):
    selectors = ['button[type="submit"]','input[type="submit"]','button:has-text("Entrar")','button:has-text("Acessar")','button:has-text("Login")','button:has-text("Logar")','button:has-text("Continuar")','input[value*="Entrar" i]','input[value*="Acessar" i]']
    for selector in selectors:
        try:
            loc = page.locator(selector)
            for i in range(loc.count()):
                if loc.nth(i).is_visible():
                    return loc.nth(i)
        except Exception:
            pass
    return None


def calcular_criticidade_e_dias(data_inicio_str):
    if not data_inicio_str:
        return "normal", 0
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


def destino_alcancado(page, destino):
    if not destino:
        return False
    atual = page.url.rstrip("/")
    alvo = destino.rstrip("/")
    return atual == alvo or alvo in atual


def clicar_link_exato(page, destino, unidade):
    if not destino:
        return False
    alvo = urlparse(destino)
    alvo_path = alvo.path.rstrip("/")
    links = page.locator("a")
    for i in range(links.count()):
        try:
            link = links.nth(i)
            if not link.is_visible():
                continue
            href = link.get_attribute("href") or ""
            parsed = urlparse(href)
            if parsed.path.rstrip("/") == alvo_path and (not alvo.netloc or not parsed.netloc or parsed.netloc == alvo.netloc):
                print(f"[{unidade}] Clicando no link correspondente ao destino: {link.inner_text(timeout=1000).strip()!r} href={href!r}")
                link.click()
                page.wait_for_timeout(3000)
                return destino_alcancado(page, destino) or alvo_path in page.url
        except Exception:
            pass
    return False


def navegar_para_destino(page, destino, unidade):
    print(f"[{unidade}] Procurando rota pela interface do CGD.")
    if clicar_link_exato(page, destino, unidade):
        print(f"[{unidade}] Destino alcançado por clique.")
        return True

    # O menu /alunos pode existir para o usuário mas negar permissão. Não insistimos nele.
    # Depois do login confirmado, a URL configurada de frequência é uma navegação normal da sessão autenticada.
    if destino:
        print(f"[{unidade}] Rota exata não está exposta no menu atual; abrindo a URL configurada dentro da sessão autenticada.")
        try:
            page.goto(destino, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            if pagina_bloqueada(page):
                print(f"[{unidade}] O destino retornou bloqueio Cloudflare.")
                salvar_diagnostico(page, unidade, "destino_bloqueado")
                return False
            if "Acesso negado" in page.locator("body").inner_text(timeout=5000):
                print(f"[{unidade}] O CGD recusou a permissão para o destino configurado.")
                salvar_diagnostico(page, unidade, "destino_acesso_negado")
                return False
            print(f"[{unidade}] Destino alcançado: {page.url}")
            return True
        except Exception as erro:
            print(f"[{unidade}] Erro navegando ao destino autenticado: {erro}")
    return False


def extrair_alunos_da_tabela(page, unidade):
    alunos = []
    if page.locator("table").count() == 0:
        print(f"[{unidade}] Nenhuma tabela encontrada.")
        return alunos
    try:
        select_limit = page.locator('select[name*="length" i],select[name*="limit" i],select[name*="per_page" i]').first
        if select_limit.is_visible(timeout=2000):
            try:
                select_limit.select_option(value="-1")
                page.wait_for_timeout(2000)
            except Exception:
                pass
    except Exception:
        pass
    pagina = 1
    while pagina <= 100:
        rows = page.locator("table tbody tr").all()
        print(f"[{unidade}] Página {pagina}: {len(rows)} linhas.")
        for indice, row in enumerate(rows):
            try:
                cols = [c.strip() for c in row.locator("td").all_text_contents()]
                if len(cols) < 3:
                    continue
                contrato, nome, curso = cols[0], cols[1], cols[2]
                status = cols[3].upper() if len(cols) > 3 else "ATIVO"
                if any(x in status for x in ["DESATIVADO", "ENCERRADO", "INATIVO"]):
                    continue
                if "informática" not in curso.lower() and "informatica" not in curso.lower():
                    continue
                data_inicio_str = cols[4] if len(cols) > 4 else ""
                criticidade, dias_ativos = calcular_criticidade_e_dias(data_inicio_str)
                criticidade_db = criticidade.lower()
                tratativa = {"critico":"aulao","moderado":"atividade_pratica","atencao":"acompanhamento"}.get(criticidade_db, "normal")
                try:
                    data_inicio_db = datetime.strptime(data_inicio_str, "%d/%m/%Y").strftime("%Y-%m-%d") if data_inicio_str else datetime.now().strftime("%Y-%m-%d")
                except Exception:
                    data_inicio_db = datetime.now().strftime("%Y-%m-%d")
                aluno = {
                    "cgd_matricula_id": contrato, "nome": nome, "contrato": contrato,
                    "email": None, "telefone": None, "curso": curso, "turma_nome": "", "professor_nome": "",
                    "data_inicio": data_inicio_db, "meses_contrato_total": 12, "ultima_aula": None, "ultimo_acesso": None,
                    "faltas_totais": 0, "faltas_mes_atual": 0, "mes_referencia_faltas": datetime.now().strftime("%m/%Y"),
                    "dias_em_curso": dias_ativos, "criticidade": criticidade_db, "tratativa_sugerida": tratativa,
                    "status_tratativa": "pendente", "status_matricula": "ativo", "bloqueado_automaticamente": False,
                    "motivo_bloqueio": None, "total_disciplinas_grade": 0, "disciplinas_concluidas": 0,
                    "unidade": "matriz" if unidade.lower() == "matriz" else "filial"
                }
                alunos.append(aluno)
                print(f"[{unidade}] ALUNO CAPTURADO: {nome}")
            except Exception as erro:
                print(f"[{unidade}] Erro linha {indice + 1}: {erro}")
        try:
            btn = page.locator('button:has-text("Próximo"),button:has-text("Próxima"),a:has-text("Próximo"),a:has-text("Próxima"),.paginate_button.next,[aria-label="Next"],[aria-label="Próxima"]').first
            if not btn.is_visible(timeout=1500):
                break
            classes = (btn.get_attribute("class") or "").lower()
            disabled = btn.is_disabled() or "disabled" in classes or (btn.get_attribute("aria-disabled") or "").lower() == "true"
            if disabled:
                break
            btn.click()
            page.wait_for_timeout(2500)
            pagina += 1
        except Exception:
            break
    return alunos


def fazer_login_e_extrair(page, usuario, senha, destino, unidade):
    print("#" * 80)
    print(f"INICIANDO PROCESSAMENTO: {unidade}")
    try:
        page.goto(CGD_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(5000)
        imprimir_estado_pagina(page, unidade, "apos_acessar_cgd")
        salvar_diagnostico(page, unidade, "apos_acessar_cgd")
        if not aguardar_acesso_legitimo(page, unidade):
            return []

        login_input, senha_input = verificar_elementos_login(page)
        if login_input and senha_input:
            print(f"[{unidade}] Campos de login encontrados; usando as credenciais configuradas.")
            login_input.fill(usuario)
            senha_input.fill(senha)
            botao = localizar_botao_login(page)
            if not botao:
                print(f"[{unidade}] ERRO: botão de login não encontrado.")
                return []
            botao.click()
            try:
                page.wait_for_load_state("networkidle", timeout=60000)
            except Exception:
                pass
            page.wait_for_timeout(5000)
            if not aguardar_acesso_legitimo(page, unidade):
                return []
            imprimir_estado_pagina(page, unidade, "apos_login")
            salvar_diagnostico(page, unidade, "apos_login")
        else:
            print(f"[{unidade}] Nenhum formulário visível: mantendo a sessão já autenticada.")

        if not navegar_para_destino(page, destino, unidade):
            return []
        page.wait_for_timeout(3000)
        imprimir_estado_pagina(page, unidade, "pagina_alunos")
        salvar_diagnostico(page, unidade, "pagina_alunos")
        alunos = extrair_alunos_da_tabela(page, unidade)
        print(f"[{unidade}] TOTAL DE ALUNOS VÁLIDOS: {len(alunos)}")
        salvar_diagnostico(page, unidade, "final")
        return alunos
    except Exception as erro:
        print(f"[{unidade}] ERRO GERAL: {erro}")
        salvar_diagnostico(page, unidade, "erro_geral")
        return []


def atualizar_supabase(alunos):
    if not alunos:
        return
    try:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        supabase.table("alunos").upsert(alunos, on_conflict="cgd_matricula_id").execute()
        print(f"Supabase: {len(alunos)} alunos enviados para alunos.")
        for unidade in ["matriz", "filial"]:
            lista = [a for a in alunos if a["unidade"] == unidade]
            resumo = {
                "unidade": unidade, "nome_unidade": "Matriz" if unidade == "matriz" else "Filial",
                "total_alunos_ativos": len(lista), "total_matriz": len(lista) if unidade == "matriz" else 0,
                "total_filial": len(lista) if unidade == "filial" else 0,
                "alunos_criticos": sum(a["criticidade"] == "critico" for a in lista),
                "alunos_moderados": sum(a["criticidade"] == "moderado" for a in lista),
                "total_contratos": len(lista), "laboratorios_ativos": [],
                "criticos": sum(a["criticidade"] == "critico" for a in lista),
                "moderados": sum(a["criticidade"] == "moderado" for a in lista),
                "atencao": sum(a["criticidade"] == "atencao" for a in lista),
                "normais": sum(a["criticidade"] == "normal" for a in lista),
                "bloqueados_faltas": sum(a["bloqueado_automaticamente"] for a in lista),
                "mes_referencia": datetime.now().strftime("%m/%Y"), "alunos_data": lista,
                "origem": "cgd_live", "ultimo_sync": datetime.now().isoformat()
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
    for nome, valor in [("CGD_LOGIN_URL", CGD_URL),("CGD_USER_MATRIZ",LOGIN_MATRIZ),("CGD_PASS_MATRIZ",SENHA_MATRIZ),("CGD_MATRIZ_URL",URL_MATRIZ),("CGD_USER_FILIAL",LOGIN_FILIAL),("CGD_PASS_FILIAL",SENHA_FILIAL),("CGD_FILIAL_URL",URL_FILIAL)]:
        print(f"{nome}: {'OK' if valor else 'AUSENTE'}")

    todos = []
    with sync_playwright() as p:
        print("Iniciando Microsoft Edge instalado com perfil persistente dedicado...")
        context = p.chromium.launch_persistent_context(user_data_dir=str(EDGE_PROFILE_DIR), channel="msedge", headless=False, viewport={"width":1440,"height":1000})
        try:
            # Uma página independente para CADA unidade. Isso impede que a sessão/permissão da Matriz seja reutilizada na Filial.
            for unidade, usuario, senha, destino in [("Matriz",LOGIN_MATRIZ,SENHA_MATRIZ,URL_MATRIZ),("Filial",LOGIN_FILIAL,SENHA_FILIAL,URL_FILIAL)]:
                if not usuario or not senha:
                    print(f"[{unidade}] credenciais ausentes; ignorada")
                    continue
                page = context.new_page()
                try:
                    todos.extend(fazer_login_e_extrair(page, usuario, senha, destino, unidade))
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
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
