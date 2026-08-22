import os
import json
import re
from playwright.sync_api import sync_playwright
from google import genai
from supabase import create_client

# Configurações de Clientes API
gemini_client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def extrair_dados_pagina(page, url_alvo):
    # Navega para a página alvo se fornecida, senão clica no menu Alunos
    if url_alvo and page.url != url_alvo:
        page.goto(url_alvo, wait_until="networkidle")
    else:
        btn_alunos = page.locator('a:has-text("Alunos")').first
        if btn_alunos.is_visible():
            btn_alunos.click()
            page.wait_for_load_state("networkidle")

    page.wait_for_timeout(4000)

    # Tenta selecionar '100' ou 'Todos' se houver seletores de tamanho sem disparar erros
    try:
        selects = page.locator("select").all()
        for sel in selects:
            if sel.is_visible():
                options = sel.locator("option").all_inner_texts()
                for opt in ["100", "500", "Todos", "All"]:
                    if opt in options:
                        sel.select_option(label=opt)
                        page.wait_for_load_state("networkidle")
                        break
    except Exception as e:
        print(f"Nota na seleção de paginação: {e}")

    page.wait_for_timeout(3000)
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
        raise ValueError("Credenciais da Matriz não encontradas nos Secrets.")

    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-infobars",
        "--window-size=1920,1080"
    ]

    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=browser_args)

        context_options = {
            "user_agent": user_agent,
            "viewport": {"width": 1920, "height": 1080},
            "locale": "pt-BR",
            "timezone_id": "America/Belem"
        }

        # 1. MATRIZ
        print("Acessando ambiente Matriz...")
        context_matriz = browser.new_context(**context_options)
        page_matriz = context_matriz.new_page()
        page_matriz.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        try:
            page_matriz.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            selector_user = 'input[name="email"], input[name="usuario"], input[name="username"], input[type="email"]'
            selector_pass = 'input[name="senha"], input[name="password"], input[type="password"]'

            page_matriz.wait_for_selector(selector_user, state="visible", timeout=30000)
            page_matriz.locator(selector_user).first.fill(user_matriz)
            page_matriz.locator(selector_pass).first.fill(pass_matriz)
            
            page_matriz.click('form#login-form button[type="submit"], form#login-form input[type="submit"], button[type="submit"]')
            page_matriz.wait_for_load_state("networkidle")

            texto_matriz = extrair_dados_pagina(page_matriz, url_matriz)
            print(f"--- MATRIZ CAPTURADA: {len(texto_matriz)} chars ---")

        except Exception as e:
            print(f"Erro no scraping da Matriz: {e}")
            raise e
        finally:
            context_matriz.close()

        # 2. FILIAL
        texto_filial = ""
        if user_filial and pass_filial:
            print("Acessando ambiente Filial...")
            context_filial = browser.new_context(**context_options)
            page_filial = context_filial.new_page()
            page_filial.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")
            
            try:
                page_filial.goto(login_url, wait_until="domcontentloaded", timeout=45000)
                page_filial.wait_for_selector(selector_user, state="visible", timeout=30000)
                page_filial.locator(selector_user).first.fill(user_filial)
                page_filial.locator(selector_pass).first.fill(pass_filial)
                
                page_filial.click('form#login-form button[type="submit"], form#login-form input[type="submit"], button[type="submit"]')
                page_filial.wait_for_load_state("networkidle")

                texto_filial = extrair_dados_pagina(page_filial, url_filial)
                print(f"--- FILIAL CAPTURADA: {len(texto_filial)} chars ---")

            except Exception as e:
                print(f"Erro no scraping da Filial: {e}")
                raise e
            finally:
                context_filial.close()

        browser.close()

    return f"""
    --- DADOS ALUNOS MATRIZ ---
    {texto_matriz[:60000]}

    --- DADOS ALUNOS FILIAL ---
    {texto_filial[:60000]}
    """

def processar_com_gemini(conteudo):
    prompt = f"""
    Você é um assistente de gestão escolar do CFIS/CGD.
    Analise o texto bruto extraído do sistema escolar abaixo.

    REGRAS DE FILTRAGEM E CONTAGEM:
    1. Identifique os alunos listados na seção MATRIZ e na seção FILIAL.
    2. Na MATRIZ: Conte apenas alunos ativos (foco em Laboratório 1 e 2).
    3. Na FILIAL: Conte alunos ativos listados.
    4. Alunos CRÍTICOS: Alunos com mais de 90 dias em curso.
    5. Alunos MODERADOS: Alunos entre 60 e 89 dias em curso.

    IMPORTANTE: Retorne ESTRITAMENTE E APENAS o JSON no formato abaixo, sem marcadores markdown como ```json:
    {{
      "total_matriz": 0,
      "total_filial": 0,
      "alunos_criticos": 0,
      "alunos_moderados": 0,
      "detalhes": [
         {{"nome": "Nome do Aluno", "unidade": "Matriz/Filial", "laboratorio": "Lab", "status": "CRÍTICO/MODERADO/NORMAL", "dias": 0}}
      ]
    }}

    Dados Brutos Extraídos:
    {conteudo}
    """
    
    response = gemini_client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt
    )
    return response.text

def salvar_no_supabase(resultado_ia):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Aviso: Variáveis do Supabase não configuradas.")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    # Tratamento rigoroso para extrair JSON da resposta
    try:
        match = re.search(r'\{.*\}', resultado_ia, re.DOTALL)
        if match:
            json_str = match.group(0)
            dados_json = json.loads(json_str)
        else:
            dados_json = json.loads(resultado_ia)
    except Exception as e:
        print(f"Erro ao converter JSON da IA: {e}")
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
    print("Dados gravados no Supabase com sucesso!")

if __name__ == "__main__":
    print("Iniciando scraping do CGD com Playwright...")
    try:
        dados = efetuar_scraping_cgd()
        print("Processando dados no Gemini...")
        resultado = processar_com_gemini(dados)
        print("Enviando dados para o Supabase...")
        salvar_no_supabase(resultado)
        print("Processo concluído com sucesso!")
    except Exception as e:
        print(f"Erro crítico durante a execução: {e}")
        raise e
