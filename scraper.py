import os
import json
import re
from playwright.sync_api import sync_playwright
from google import genai
from supabase import create_client

gemini_client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def extrair_dados_unidade(page, target_url):
    # Se houver uma URL direta do relatório/tabela, navega até ela
    if target_url and target_url.strip():
        print(f"Navegando diretamente para a URL: {target_url}")
        page.goto(target_url, wait_until="networkidle", timeout=60000)
    else:
        # Tentativa de clique em links comuns de menu caso não haja URL direta
        print("Buscando menu de alunos...")
        for selector in ['a:has-text("Alunos")', 'a:has-text("Relatórios")', 'a[href*="aluno"]']:
            try:
                if page.locator(selector).first.is_visible():
                    page.locator(selector).first.click()
                    page.wait_for_load_state("networkidle")
                    break
            except Exception:
                pass

    page.wait_for_timeout(5000)

    # Captura todas as tabelas encontradas na página
    tables_html = page.locator("table").all_inner_texts()
    if tables_html:
        print(f"Encontradas {len(tables_html)} tabela(s) na página.")
        conteudo_tabelas = "\n--- TABELA ---\n".join(tables_html)
        return conteudo_tabelas
    
    # Se não achar tags <table>, pega todo o texto útil da página
    print("Nenhuma tag <table> explícita encontrada. Extraindo texto do body...")
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
        raise ValueError("Credenciais ou URL de login ausentes nos Secrets.")

    browser_args = ["--no-sandbox", "--disable-setuid-sandbox", "--window-size=1920,1080"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=browser_args)
        
        # 1. SCRAPING MATRIZ
        print("--- INICIANDO MATRIZ ---")
        context_matriz = browser.new_context(viewport={"width": 1920, "height": 1080})
        page_m = context_matriz.new_page()
        
        page_m.goto(login_url, wait_until="domcontentloaded", timeout=45000)
        
        # Login
        page_m.fill('input[name="email"], input[name="usuario"], input[type="text"], input[type="email"]', user_matriz)
        page_m.fill('input[name="senha"], input[name="password"], input[type="password"]', pass_matriz)
        page_m.click('button[type="submit"], input[type="submit"]')
        page_m.wait_for_load_state("networkidle")
        
        texto_matriz = extrair_dados_unidade(page_m, url_matriz)
        print(f"Tamanho do conteúdo extraído da Matriz: {len(texto_matriz)} caracteres")
        context_matriz.close()

        # 2. SCRAPING FILIAL
        texto_filial = ""
        if user_filial and pass_filial:
            print("--- INICIANDO FILIAL ---")
            context_filial = browser.new_context(viewport={"width": 1920, "height": 1080})
            page_f = context_filial.new_page()
            
            page_f.goto(login_url, wait_until="domcontentloaded", timeout=45000)
            page_f.fill('input[name="email"], input[name="usuario"], input[type="text"], input[type="email"]', user_filial)
            page_f.fill('input[name="senha"], input[name="password"], input[type="password"]', pass_filial)
            page_f.click('button[type="submit"], input[type="submit"]')
            page_f.wait_for_load_state("networkidle")
            
            texto_filial = extrair_dados_unidade(page_f, url_filial)
            print(f"Tamanho do conteúdo extraído da Filial: {len(texto_filial)} caracteres")
            context_filial.close()

        browser.close()

    return f"""
    === DADOS UNIDADE MATRIZ ===
    {texto_matriz[:80000]}

    === DADOS UNIDADE FILIAL ===
    {texto_filial[:80000]}
    """

def processar_com_gemini(conteudo):
    prompt = f"""
    Você é um analisador de dados escolares.
    Analise a listagem abaixo obtida das unidades Matriz e Filial.

    INSTRUÇÕES:
    1. Conte e liste todos os alunos reais presentes no texto.
    2. Calcule os totais para Matriz e Filial.
    3. Identifique alunos CRÍTICOS (>90 dias em curso) e MODERADOS (60 a 89 dias).

    Responda APENAS com o JSON no seguinte formato (sem formatação extra em markdown):
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
    print("Gravação no Supabase finalizada com sucesso!")

if __name__ == "__main__":
    print("Executando automação de extração do CGD...")
    dados = efetuar_scraping_cgd()
    print("Enviando dados para o Gemini...")
    resultado = processar_com_gemini(dados)
    print("Atualizando banco de dados no Supabase...")
    salvar_no_supabase(resultado)
    print("Concluído!")
