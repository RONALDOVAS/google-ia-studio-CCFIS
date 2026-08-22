import os
import json
import re
from playwright.sync_api import sync_playwright
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def extrair_via_interceptacao(browser, url_login, usuario, senha, nome_unidade):
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    page = context.new_page()
    
    dados_interceptados = []

    # Escuta respostas de rede XHR/Fetch para capturar dados completos
    def tratar_resposta(response):
        if "aluno" in response.url.lower() or "dados" in response.url.lower() or "json" in response.headers.get("content-type", ""):
            try:
                json_body = response.json()
                if isinstance(json_body, dict) and ("data" in json_body or "aaData" in json_body):
                    lista = json_body.get("data") or json_body.get("aaData")
                    if isinstance(lista, list) and len(lista) > 0:
                        dados_interceptados.extend(lista)
            except Exception:
                pass

    page.on("response", tratar_resposta)

    try:
        print(f"[{nome_unidade}] Efetuando login...")
        page.goto(url_login, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        page.locator('#login-email, input[name="email"], input[type="email"]').first.fill(usuario)
        page.locator('input[name="password"], input[type="password"]').first.fill(senha)

        submit_btn = page.locator('button[type="submit"], input[type="submit"]').first
        if submit_btn.is_visible():
            submit_btn.click()
        else:
            page.keyboard.press("Enter")

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000)

        # Clica no menu Alunos
        menu_alunos = page.locator('a:has-text("Alunos"), span:has-text("Alunos")').first
        if menu_alunos.is_visible():
            menu_alunos.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(5000)

        # Tenta selecionar 'Todos' ou mudar a quantidade exibida
        try:
            selects = page.locator('select').all()
            for sel in selects:
                if sel.is_visible():
                    options = sel.locator("option").all_inner_texts()
                    for target in ["100", "500", "todos", "all"]:
                        matched = [o for o in options if target in o.lower()]
                        if matched:
                            sel.select_option(label=matched[0])
                            page.wait_for_timeout(5000)
                            break
        except Exception as e:
            print(f"[{nome_unidade}] Aviso ao alterar seletor: {e}")

        # Se não interceptou via API, faz leitura direta no DOM em todas as tabelas e iFrames
        if not dados_interceptados:
            print(f"[{nome_unidade}] Extraindo registros do DOM...")
            frames = page.frames
            for frame in frames:
                try:
                    linhas = frame.evaluate("""() => {
                        return Array.from(document.querySelectorAll('tr')).map(r => {
                            const tds = Array.from(r.querySelectorAll('td')).map(td => td.innerText.trim());
                            return tds.length >= 2 ? tds : null;
                        }).filter(Boolean);
                    }""")
                    if linhas:
                        dados_interceptados.extend(linhas)
                except Exception:
                    pass

        alunos_formatados = []
        for i, item in enumerate(dados_interceptados):
            if isinstance(item, list):
                contrato = item[0] if len(item) > 0 else f"{i}"
                nome = item[1] if len(item) > 1 else "Aluno"
                curso = item[2] if len(item) > 2 else ""
                dias_str = item[3] if len(item) > 3 else "0"
            elif isinstance(item, dict):
                contrato = item.get("contrato") or item.get("id") or f"{i}"
                nome = item.get("nome") or item.get("aluno") or "Aluno"
                curso = item.get("curso") or ""
                dias_str = str(item.get("dias") or item.get("ultimo_acesso") or "0")
            else:
                continue

            match_dias = re.search(r'\d+', dias_str)
            dias = int(match_dias.group(0)) if match_dias else 0

            status = "NORMAL"
            if dias > 90:
                status = "CRÍTICO"
            elif dias >= 60:
                status = "MODERADO"
            elif dias >= 30:
                status = "ATENÇÃO"

            alunos_formatados.append({
                "contrato": str(contrato),
                "nome": str(nome),
                "unidade": nome_unidade,
                "curso": str(curso),
                "status": status,
                "dias": dias,
                "faltas": 0
            })

        print(f"[{nome_unidade}] Total extraído com sucesso: {len(alunos_formatados)} alunos.")
        return alunos_formatados

    except Exception as e:
        print(f"[{nome_unidade}] Erro na execução: {e}")
        return []
    finally:
        context.close()

def efetuar_scraping_cgd():
    login_url = os.environ.get("CGD_LOGIN_URL")
    user_matriz = os.environ.get("CGD_USER_MATRIZ")
    pass_matriz = os.environ.get("CGD_PASS_MATRIZ")
    user_filial = os.environ.get("CGD_USER_FILIAL")
    pass_filial = os.environ.get("CGD_PASS_FILIAL") or os.environ.get("CDG_PASS_FILIAL")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])

        alunos_matriz = extrair_via_interceptacao(browser, login_url, user_matriz, pass_matriz, "Matriz")
        alunos_filial = []
        if user_filial and pass_filial:
            alunos_filial = extrair_via_interceptacao(browser, login_url, user_filial, pass_filial, "Filial")

        browser.close()

    return {
        "total_matriz": len(alunos_matriz),
        "total_filial": len(alunos_filial),
        "detalhes": alunos_matriz + alunos_filial
    }

def salvar_no_supabase(dados):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Supabase não configurado.")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    detalhes = dados.get("detalhes", [])
    criticos = sum(1 for a in detalhes if a.get("status") == "CRÍTICO")
    moderados = sum(1 for a in detalhes if a.get("status") == "MODERADO")

    payload = {
        "id": 1,
        "relatorio": json.dumps(dados, ensure_ascii=False),
        "total_filial": dados.get("total_filial", 0),
        "total_matriz": dados.get("total_matriz", 0),
        "alunos_criticos": criticos,
        "alunos_moderados": moderados,
        "dados_completos": detalhes,
        "atualizado_em": "now()"
    }

    supabase.table("resumo_cgd").upsert(payload).execute()
    print("Sucesso: Supabase atualizado com dados interceptados do sistema!")

if __name__ == "__main__":
    print("Iniciando extração via interceptação de rede...")
    dados = efetuar_scraping_cgd()
    print(f"Total geral capturado: {len(dados['detalhes'])} alunos.")
    salvar_no_supabase(dados)
    print("Finalizado!")
