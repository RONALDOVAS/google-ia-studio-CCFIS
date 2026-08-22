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
    # Navega diretamente para a URL de alunos se estiver definida
    if url_alvo and url_alvo.strip() and url_alvo != page.url:
        page.goto(url_alvo, wait_until="domcontentloaded", timeout=60000)
    else:
        for selector in ['a:has-text("Alunos")', 'a:has-text("Relatórios")', 'a[href*="aluno"]']:
            try:
                loc = page.locator(selector).first
                if loc.is_visible():
                    loc.click()
                    page.wait_for_load_state("domcontentloaded")
                    break
            except Exception:
                pass

    # Aguarda explicitamente a tabela ou a lista de alunos carregar no DOM
    try:
        page.wait_for_selector('table, .dataTables_wrapper, .table-responsive, div[class*="aluno"]', state="visible", timeout=20000)
    except Exception as e:
        print(f"Aviso ao aguardar tabela de alunos: {e}")

    page.wait_for_timeout(5000)

    # Tenta alterar a paginação para exibir todos os alunos na mesma lista
    try:
        dropdowns = page.locator("select").all()
        for sel in dropdowns:
            if sel.is_visible():
                options = sel.locator("option").all_inner_texts()
                for target in ["100", "500", "Todos", "All"]:
                    matched = [o for o in options if target.lower() in o.lower()]
                    if matched:
                        sel.select_option(label=matched[0])
                        page.wait_for_timeout(3000)
                        break
    except Exception as e:
        print(f"Aviso na alteração da paginação: {e}")

    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)

    texto = page.inner_text("body")
    print(f"--- Prévia do Texto Extraído ({len(texto)} chars) ---")
    print(texto[:500])
    print("-----------------------------------------------------")
    return texto

def efetuar_login_e_extrair(browser, url_login, usuario, senha, url_alvo):
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    try:
        page.goto(url_login, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        user_input = page.locator('input[type="text"], input[type="email"], input[name*="user"], input[name*="login"], input[name*="email"]').first
        pass_input = page.locator('input[type="password"], input[name*="senha"], input[name*="pass"]').first

        user_input.fill(usuario)
        pass_input.fill(senha)

        submit_btn = page.locator('button[type="submit"], input[type="submit"], button:has-text("Entrar"), button:has-text("Acessar")').first
        if submit_btn.is_visible():
            submit_btn.click()
        else:
            pass_input.press("Enter")

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(5000)

        return extrair_pagina_alunos(page, url_alvo)
    finally:
        context.close()

def efetuar_scraping_cgd():
    login_url = os.environ.get("CGD_LOGIN_URL")
    url_matriz = os.environ.get("CGD_MATRIZ_URL")
    url_filial = os.environ.get("CGD_FILIAL_URL")
    
    user_matriz = os.environ.get("CGD_USER_MATRIZ")
    pass_matriz = os.environ.get("CGD_PASS_MATRIZ")
    
    user_filial = os.environ.get("CGD_USER_FILIAL")
    pass_filial = os.environ.get("CGD_PASS_FILIAL") or os.environ.get("CDG_PASS_FILIAL")

    if not login_url or not user_matriz or not pass_matriz:
        raise ValueError("Credenciais de login ausentes nos Secrets.")

    browser_args = [
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-dev-shm-usage",
        "--disable-gpu"
    ]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=browser_args)

        print("Processando Matriz...")
        texto_matriz = efetuar_login_e_extrair(browser, login_url, user_matriz, pass_matriz, url_matriz)

        texto_filial = ""
        if user_filial and pass_filial:
            print("Processando Filial...")
            texto_filial = efetuar_login_e_extrair(browser, login_url, user_filial, pass_filial, url_filial)

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
    2. Na MATRIZ: Conte apenas alunos ativos.
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
    print("Processando com Gemini...")
    resultado = processar_com_gemini(dados)
    print("Atualizando Supabase...")
    salvar_no_supabase(resultado)
    print("Concluído!")
