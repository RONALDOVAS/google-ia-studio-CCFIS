import os
import json
import re
from playwright.sync_api import sync_playwright
from google import genai
from supabase import create_client

gemini_client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def navegar_e_extrair(page, target_url):
    # Se houver URL específica, vai direto; caso contrário navega pelos menus
    if target_url and target_url.strip():
        page.goto(target_url, wait_until="networkidle", timeout=45000)
    
    page.wait_for_timeout(3000)

    # Clica na aba ou menu de Alunos/Relatórios caso esteja na Dashboard
    for selector in ['a:has-text("Alunos")', 'a:has-text("Relatório")', 'a[href*="aluno"]']:
        try:
            loc = page.locator(selector).first
            if loc.is_visible():
                loc.click()
                page.wait_for_load_state("networkidle")
                break
        except Exception:
            pass

    page.wait_for_timeout(4000)

    # Rola a página para garantir carregamento de registros em lazy loading
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)

    # Retorna o texto bruto capturado da página
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
        raise ValueError("Credenciais de login ausentes nos Secrets.")

    browser_args = ["--no-sandbox", "--disable-setuid-sandbox", "--window-size=1920,1080"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=browser_args)

        # 1. SCRAPING MATRIZ
        print("--- INICIANDO MATRIZ ---")
        context_matriz = browser.new_context(viewport={"width": 1920, "height": 1080})
        page_m = context_matriz.new_page()
        
        page_m.goto(login_url, wait_until="domcontentloaded", timeout=45000)
        
        # Seletores flexíveis de login
        user_input = page_m.locator('form#login-form input[type="text"], form#login-form input[type="email"], input[name="email"], input[name="usuario"]').first
        pass_input = page_m.locator('form#login-form input[type="password"], input[name="senha"], input[name="password"]').first
        
        user_input.wait_for(state="visible", timeout=30000)
        user_input.fill(user_matriz)
        pass_input.fill(pass_matriz)
        
        submit_btn = page_m.locator('form#login-form button[type="submit"], form#login-form input[type="submit"], button[type="submit"]').first
        submit_btn.click()
        page_m.wait_for_load_state("networkidle")
        
        texto_matriz = navegar_e_extrair(page_m, url_matriz)
        print(f"Texto extraído Matriz: {len(texto_matriz)} caracteres")
        context_matriz.close()

        # 2. SCRAPING FILIAL
        texto_filial = ""
        if user_filial and pass_filial:
            print("--- INICIANDO FILIAL ---")
            context_filial = browser.new_context(viewport={"width": 1920, "height": 1080})
            page_f = context_filial.new_page()
            
            page_f.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            
            u_input_f = page_f.locator('form#login-form input[type="text"], form#login-form input[type="email"], input[name="email"], input[name="usuario"]').first
            p_input_f = page_f.locator('form#login-form input[type="password"], input[name="senha"], input[name="password"]').first
            
            u_input_f.wait_for(state="visible", timeout=30000)
            u_input_f.fill(user_filial)
            p_input_f.fill(pass_filial)
            
            sub_btn_f = page_f.locator('form#login-form button[type="submit"], form#login-form input[type="submit"], button[type="submit"]').first
            sub_btn_f.click()
            page_f.wait_for_load_state("networkidle")
            
            texto_filial = navegar_e_extrair(page_f, url_filial)
            print(f"Texto extraído Filial: {len(texto_filial)} caracteres")
            context_filial.close()

        browser.close()

    return f"""
    === DADOS UNIDADE MATRIZ ===
    {texto_matriz}

    === DADOS UNIDADE FILIAL ===
    {texto_filial}
    """

def processar_com_gemini(conteudo):
    prompt = f"""
    Você é um assistente de gestão escolar do CFIS/CGD.
    Analise o texto bruto extraído abaixo.

    REGRAS:
    1. Identifique e conte TODOS os alunos individuais listados na seção MATRIZ e na FILIAL.
    2. Na MATRIZ: Conte apenas alunos ativos (Laboratório 1 e 2).
    3. Na FILIAL: Conte alunos ativos listados.
    4. Alunos CRÍTICOS: >90 dias em curso.
    5. Alunos MODERADOS: entre 60 e 89 dias em curso.

    Responda EXCLUSIVAMENTE um JSON sem sintaxe markdown extra:
    {{
      "total_matriz": 0,
      "total_filial": 0,
      "alunos_criticos": 0,
      "alunos_moderados": 0,
      "detalhes": [
         {{"nome": "Nome do Aluno", "unidade": "Matriz/Filial", "laboratorio": "Lab", "status": "CRÍTICO/MODERADO/NORMAL", "dias": 0}}
      ]
    }}

    Dados Extraídos:
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
        print(f"Erro ao processar JSON da IA: {e}")
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
    print("Dados gravados com sucesso no Supabase!")

if __name__ == "__main__":
    print("Iniciando execução...")
    dados = efetuar_scraping_cgd()
    print("Enviando dados para o Gemini...")
    resultado = processar_com_gemini(dados)
    print("Salvando no Supabase...")
    salvar_no_supabase(resultado)
    print("Processo concluído!")
