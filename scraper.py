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
    
    # Busca credenciais específicas de cada unidade
    user_matriz = os.environ.get("CGD_USER_MATRIZ")
    pass_matriz = os.environ.get("CGD_PASS_MATRIZ")
    
    user_filial = os.environ.get("CGD_USER_FILIAL")
    pass_filial = os.environ.get("CGD_PASS_FILIAL") or os.environ.get("CDG_PASS_FILIAL")

    print(f"DEBUG - LOGIN_URL: {bool(login_url)}")
    print(f"DEBUG - MATRIZ_URL: {bool(url_matriz)} | USER_MATRIZ: {bool(user_matriz)} | PASS_MATRIZ: {bool(pass_matriz)}")
    print(f"DEBUG - FILIAL_URL: {bool(url_filial)} | USER_FILIAL: {bool(user_filial)} | PASS_FILIAL: {bool(pass_filial)}")

    if not login_url or not user_matriz or not pass_matriz:
        raise ValueError("Credenciais da Matriz não encontradas nos Secrets.")

    # Argumentos do Chromium para simular navegação comum e desativar marcações de robô
    browser_args = [
        "--disable-blink-features=AutomationControlled",
        "--no-sandbox",
        "--disable-setuid-sandbox",
        "--disable-infobars",
        "--window-size=1920,1080"
    ]

    # User-Agent real do Chrome desktop
    user_agent = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=browser_args)

        # Configura o contexto simulando uma sessão real
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
        
        # Oculta a flag 'navigator.webdriver = true'
        page_matriz.add_init_script("Object.defineProperty(navigator, 'webdriver', {get: () => undefined})")

        try:
            page_matriz.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            page_matriz.wait_for_timeout(3000) # Aguarda renderização de proteções dinâmicas
            
            # Aguarda a presença de inputs na tela
            page_matriz.wait_for_selector('input', timeout=20000)
            
            # Preenche o usuário
            inputs_texto = page_matriz.locator('input[type="text"], input[type="email"], input:not([type="hidden"]):not([type="submit"]):not([type="password"])')
            inputs_texto.first.fill(user_matriz)
            
            # Preenche a senha
            page_matriz.locator('input[type="password"]').first.fill(pass_matriz)
            
            # Submete o formulário
            page_matriz.click('button[type="submit"], input[type="submit"], button:has-text("Entrar"), button:has-text("Login")')
            page_matriz.wait_for_load_state("networkidle")

        except Exception as e:
            print(f"\n--- DIAGNÓSTICO DE ERRO NO LOGIN ---")
            print(f"URL onde a página parou: {page_matriz.url}")
            print(f"Título da página: {page_matriz.title()}")
            print("Trecho do código HTML da página no momento do erro:")
            print(page_matriz.content()[:3000])
            print("-----------------------------------\n")
            raise e

        if url_matriz and url_matriz != login_url:
            page_matriz.goto(url_matriz, wait_until="networkidle")
        
        texto_matriz = page_matriz.inner_text("body")
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
                page_filial.wait_for_timeout(3000)
                page_filial.wait_for_selector('input', timeout=20000)
                
                inputs_texto_f = page_filial.locator('input[type="text"], input[type="email"], input:not([type="hidden"]):not([type="submit"]):not([type="password"])')
                inputs_texto_f.first.fill(user_filial)
                page_filial.locator('input[type="password"]').first.fill(pass_filial)
                page_filial.click('button[type="submit"], input[type="submit"], button:has-text("Entrar"), button:has-text("Login")')
                page_filial.wait_for_load_state("networkidle")
                
                if url_filial and url_filial != login_url:
                    page_filial.goto(url_filial, wait_until="networkidle")
                
                texto_filial = page_filial.inner_text("body")
                context_filial.close()
            except Exception as e:
                print(f"\n--- DIAGNÓSTICO DE ERRO NO LOGIN (FILIAL) ---")
                print(f"URL onde a página parou: {page_filial.url}")
                print(f"Título da página: {page_filial.title()}")
                print("Trecho do código HTML da página no momento do erro:")
                print(page_filial.content()[:3000])
                print("-----------------------------------\n")
                raise e
        else:
            print("Aviso: Credenciais da Filial não configuradas. Pulando etapa Filial.")

        browser.close()

    return f"""
    --- DADOS ALUNOS MATRIZ ---
    {texto_matriz[:15000]}

    --- DADOS ALUNOS FILIAL ---
    {texto_filial[:15000]}
    """

def processar_com_gemini(conteudo):
    prompt = f"""
    Você é um assistente de gestão escolar do CFIS/CGD.
    Analise os dados extraídos das páginas do sistema CGD.

    REGRAS DE FILTRAGEM OBRIGATÓRIAS:
    1. Na MATRIZ: Considere APENAS alunos ATIVOS vinculados ao "Laboratório 1" ou "Laboratório 2".
    2. Na FILIAL: Considere os alunos ativos conforme a listagem de turmas da filial.
    3. Alunos críticos: Mais de 90 dias em curso sem conclusão.
    4. Alunos moderados: Entre 60 e 89 dias em curso.

    IMPORTANTE: Responda ESTRITAMENTE em formato JSON (sem marcadores adicionais), com a estrutura exata:
    {{
      "total_matriz": número_de_alunos,
      "total_filial": número_de_alunos,
      "alunos_criticos": número_de_alunos,
      "alunos_moderados": número_de_alunos,
      "detalhes": [
         {{"nome": "Nome do Aluno", "unidade": "Matriz/Filial", "laboratorio": "Lab 1/Lab 2", "status": "CRÍTICO/MODERADO/NORMAL", "dias": 90}}
      ]
    }}

    Dados Brutos:
    {conteudo}
    """
    
    response = gemini_client.models.generate_content(
        model='gemini-2.5-flash',
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
