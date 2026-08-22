import os
import json
from playwright.sync_api import sync_playwright
from google import genai
from supabase import create_client

# Configurações de Clientes API
gemini_client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

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

        # 1. RASPAGEM DA MATRIZ
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

            # Navegação direcionada aos elementos do menu CGD
            if page_matriz.locator("#relatorioAlunosSB").is_visible():
                page_matriz.locator("#relatorioAlunosSB").click()
                page_matriz.wait_for_load_state("networkidle")
            elif page_matriz.locator('span.title:has-text("Alunos")').is_visible():
                page_matriz.locator('span.title:has-text("Alunos")').first.click()
                page_matriz.wait_for_load_state("networkidle")
            elif url_matriz and url_matriz != login_url:
                page_matriz.goto(url_matriz, wait_until="networkidle")

            page_matriz.wait_for_timeout(6000)
            texto_matriz = page_matriz.inner_text("body")
            
            print(f"--- PREVIEW TEXTO BRUTO MATRIZ ({len(texto_matriz)} chars) ---")
            print(texto_matriz[:1000])

        except Exception as e:
            print(f"Erro no scraping da Matriz: {e}")
            raise e
        finally:
            context_matriz.close()

        # 2. RASPAGEM DA FILIAL
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

                if page_filial.locator("#relatorioAlunosSB").is_visible():
                    page_filial.locator("#relatorioAlunosSB").click()
                    page_filial.wait_for_load_state("networkidle")
                elif page_filial.locator('span.title:has-text("Alunos")').is_visible():
                    page_filial.locator('span.title:has-text("Alunos")').first.click()
                    page_filial.wait_for_load_state("networkidle")
                elif url_filial and url_filial != login_url:
                    page_filial.goto(url_filial, wait_until="networkidle")
                
                page_filial.wait_for_timeout(6000)
                texto_filial = page_filial.inner_text("body")

                print(f"--- PREVIEW TEXTO BRUTO FILIAL ({len(texto_filial)} chars) ---")
                print(texto_filial[:1000])

            except Exception as e:
                print(f"Erro no scraping da Filial: {e}")
                raise e
            finally:
                context_filial.close()

        browser.close()

    return f"""
    --- DADOS ALUNOS MATRIZ ---
    {texto_matriz[:20000]}

    --- DADOS ALUNOS FILIAL ---
    {texto_filial[:20000]}
    """

def processar_com_gemini(conteudo):
    prompt = f"""
    Você é um assistente de gestão escolar do CFIS/CGD.
    Analise o texto bruto extraído do sistema escolar abaixo.

    REGRAS DE FILTRAGEM E CONTAGEM:
    1. Identifique os alunos presentes e seus respectivos tempos de curso/status.
    2. Na MATRIZ: Conte apenas alunos ativos (foco em Laboratório 1 e 2).
    3. Na FILIAL: Conte alunos ativos listados.
    4. Alunos CRÍTICOS: Alunos com mais de 90 dias em curso.
    5. Alunos MODERADOS: Alunos entre 60 e 89 dias em curso.

    IMPORTANTE: Retorne APENAS um JSON válido, sem texto explicativo adicional:
    {{
      "total_matriz": 0,
      "total_filial": 0,
      "alunos_criticos": 0,
      "alunos_moderados": 0,
      "detalhes": [
         {{"nome": "Nome", "unidade": "Matriz/Filial", "laboratorio": "Lab", "status": "CRÍTICO/MODERADO/NORMAL", "dias": 0}}
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
    
    try:
        texto_limpo = resultado_ia.replace("```json", "").replace("```", "").strip()
        dados_json = json.loads(texto_limpo)
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
