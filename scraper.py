import os
import json
import re
from playwright.sync_api import sync_playwright
from google import genai
from supabase import create_client

gemini_client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def extrair_pagina_alunos(page, url_alvo):
    # Se houver uma sub-URL direta, acessa ela
    if url_alvo and url_alvo.strip() and url_alvo != page.url:
        page.goto(url_alvo, wait_until="networkidle", timeout=45000)
    else:
        btn_alunos = page.locator('a:has-text("Alunos")').first
        if btn_alunos.is_visible():
            btn_alunos.click()
            page.wait_for_load_state("networkidle")

    page.wait_for_timeout(4000)

    # Tenta alterar a paginação da tabela para mostrar o máximo de registros
    try:
        dropdowns = page.locator("select").all()
        for sel in dropdowns:
            if sel.is_visible():
                options = sel.locator("option").all_inner_texts()
                for target in ["100", "500", "Todos", "All"]:
                    matched = [o for o in options if target.lower() in o.lower()]
                    if matched:
                        sel.select_option(label=matched[0])
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(3000)
                        break
    except Exception as e:
        print(f"Aviso ao tentar mudar paginação: {e}")

    # Rola até o fim para carregar dados dinâmicos
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)

    return page.inner_text("body")

def efetuar_scraping_cgd():
    login_url = os.environ.get("CGD_LOGIN_URL")
    url_matriz = os.environ.get("CGD_MATRIZ_URL")
    url_filial = os.environ.get("CGD_FILIAL_URL")
    
    user_matriz = os.environ.get("CGD_USER_MATRIZ")
    pass_matriz = os.environ.get("CGD_PASS_MATRIZ")
    
    user_filial = os.environ.get("CGD_USER_FILIAL")
    pass_filial = os.environ.get("CGD_PASS_FILIAL") or os.environ.get("CDG_PASS_FILIAL")

    if not login_url or not user_matriz or not pass_matriz:
        raise ValueError("Credenciais ou URL de login não configuradas nos Secrets.")

    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--window-size=1920,1080"
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=browser_args)

        # 1. MATRIZ
        print("Acessando Matriz...")
        ctx_matriz = browser.new_context(viewport={"width": 1920, "height": 1080})
        page_m = ctx_matriz.new_page()

        try:
            page_m.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            
            # Aguarda explicitamente os campos de login estarem visíveis
            page_m.wait_for_selector('input[type="password"], input[name="senha"]', state="visible", timeout=30000)
            
            input_user = page_m.locator('input[name="email"], input[name="usuario"], input[type="email"], input[type="text"]').first
            input_pass = page_m.locator('input[name="senha"], input[name="password"], input[type="password"]').first

            input_user.fill(user_matriz)
            input_pass.fill(pass_matriz)
            
            page_m.click('button[type="submit"], input[type="submit"]')
            page_m.wait_for_load_state("networkidle")

            texto_matriz = extrair_pagina_alunos(page_m, url_matriz)
            print(f"Matriz extraída: {len(texto_matriz)} caracteres.")

        except Exception as e:
            print(f"Erro na Matriz: {e}")
            raise e
        finally:
            ctx_matriz.close()

        # 2. FILIAL
        texto_filial = ""
        if user_filial and pass_filial:
            print("Acessando Filial...")
            ctx_filial = browser.new_context(viewport={"width": 1920, "height": 1080})
            page_f = ctx_filial.new_page()

            try:
                page_f.goto(login_url, wait_until="domcontentloaded", timeout=45000)
                page_f.wait_for_selector('input[type="password"], input[name="senha"]', state="visible", timeout=30000)
                
                input_user_f = page_f.locator('input[name="email"], input[name="usuario"], input[type="email"], input[type="text"]').first
                input_pass_f = page_f.locator('input[name="senha"], input[name="password"], input[type="password"]').first

                input_user_f.fill(user_filial)
                input_pass_f.fill(pass_filial)

                page_f.click('button[type="submit"], input[type="submit"]')
                page_f.wait_for_load_state("networkidle")

                texto_filial = extrair_pagina_alunos(page_f, url_filial)
                print(f"Filial extraída: {len(texto_filial)} caracteres.")

            except Exception as e:
                print(f"Erro na Filial: {e}")
                raise e
            finally:
                ctx_filial.close()

        browser.close()

    return f"""
    === DADOS MATRIZ ===
    {texto_matriz[:80000]}

    === DADOS FILIAL ===
    {texto_filial[:80000]}
    """

def processar_com_gemini(conteudo):
    prompt = f"""
    Você é um assistente de gestão escolar do CFIS/CGD.
    Analise o texto bruto extraído abaixo das páginas de alunos.

    REGRAS DE PROCESSAMENTO:
    1. Identifique a quantidade TOTAL real de alunos listados na MATRIZ e na FILIAL.
    2. Na MATRIZ: Conte apenas alunos ativos (Laboratório 1 e 2).
    3. Na FILIAL: Conte alunos ativos.
    4. Alunos CRÍTICOS: Alunos com mais de 90 dias em curso.
    5. Alunos MODERADOS: Alunos entre 60 e 89 dias em curso.

    Responda EXCLUSIVAMENTE um JSON sem sintaxe markdown:
    {{
      "total_matriz": 0,
      "total_filial": 0,
      "alunos_criticos": 0,
      "alunos_moderados": 0,
      "detalhes": [
         {{"nome": "Nome do Aluno", "unidade": "Matriz/Filial", "laboratorio": "Lab", "status": "CRÍTICO/MODERADO/NORMAL", "dias": 0}}
      ]
    }}

    CONTEÚDO PARA ANÁLISE:
    {conteudo}
    """
    
    response = gemini_client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt
    )
    return response.text

def salvar_no_supabase(resultado_ia):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Aviso: Supabase não configurado.")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    try:
        match = re.search(r'\{.*\}', resultado_ia, re.DOTALL)
        if match:
            dados_json = json.loads(match.group(0))
        else:
            dados_json = json.loads(resultado_ia)
    except Exception as e:
        print(f"Erro no parsing do JSON: {e}")
        dados_json = {}

    payload = {
        "id": 1,
        "relatorio": resultado_ia,
        "total_filial": dados_json.get("total_filial", 0),
        "total_matriz": dados_json.get("total_matriz", 0),
        "alunos_criticos": dados_json.get("alunos_criticos", 0),
        "alunos_moderados": dados_json.get("alunos_moderados", 0),
        "dados_completos": dados_json.get("detalhes", []),
        "atualizado_em": "now()"
    }

    supabase.table("resumo_cgd").upsert(payload).execute()
    print("Sucesso! Registro atualizado no Supabase.")

if __name__ == "__main__":
    print("Iniciando execução...")
    dados = efetuar_scraping_cgd()
    print("Processando via Gemini...")
    resultado = processar_com_gemini(dados)
    print("Atualizando banco de dados...")
    salvar_no_supabase(resultado)
    print("Concluído!")
