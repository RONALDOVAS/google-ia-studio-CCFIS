import os
import json
import re
from playwright.sync_api import sync_playwright
from google import genai
from supabase import create_client

gemini_client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def extrair_dados_completos(page, url_destino):
    if url_destino and url_destino.strip():
        page.goto(url_destino, wait_until="networkidle", timeout=45000)
    
    page.wait_for_timeout(3000)

    # Navega para o menu de alunos se necessário
    for selector in ['a:has-text("Alunos")', 'a:has-text("Relatórios")', 'a[href*="aluno"]']:
        try:
            loc = page.locator(selector).first
            if loc.is_visible():
                loc.click()
                page.wait_for_load_state("networkidle")
                break
        except Exception:
            pass

    page.wait_for_timeout(4000)

    # Tenta alterar a paginação para exibir o máximo de alunos por página
    try:
        selects = page.locator("select").all()
        for sel in selects:
            if sel.is_visible():
                options = sel.locator("option").all_inner_texts()
                for opt in ["100", "500", "Todos", "All"]:
                    if any(opt.lower() in o.lower() for o in options):
                        sel.select_option(label=opt)
                        page.wait_for_load_state("networkidle")
                        page.wait_for_timeout(3000)
                        break
    except Exception as e:
        print(f"Nota na seleção de paginação: {e}")

    # Rola a página para garantir o carregamento
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    page.wait_for_timeout(2000)

    return page.inner_text("body")

def efetuar_login_e_scraping(page, url_login, usuario, senha, url_alvo):
    page.goto(url_login, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_timeout(2000)

    # Busca genérica para o campo de usuário
    user_field = None
    for sel in ['input[type="text"]', 'input[type="email"]', 'input[name*="user"]', 'input[name*="login"]', 'input[name*="email"]', 'input']:
        try:
            if page.locator(sel).first.is_visible():
                user_field = page.locator(sel).first
                break
        except Exception:
            pass

    # Busca genérica para o campo de senha
    pass_field = None
    for sel in ['input[type="password"]', 'input[name*="senha"]', 'input[name*="pass"]']:
        try:
            if page.locator(sel).first.is_visible():
                pass_field = page.locator(sel).first
                break
        except Exception:
            pass

    if not user_field or not pass_field:
        raise ValueError("Não foi possível localizar os campos de login na página.")

    user_field.fill(usuario)
    pass_field.fill(senha)

    # Clique no botão de submissão
    btn_submit = page.locator('button[type="submit"], input[type="submit"], button:has-text("Entrar"), button:has-text("Acessar")').first
    btn_submit.click()
    page.wait_for_load_state("networkidle")

    return extrair_dados_completos(page, url_alvo)

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

        # MATRIZ
        print("--- PROCESSANDO MATRIZ ---")
        ctx_matriz = browser.new_context(viewport={"width": 1920, "height": 1080})
        page_m = ctx_matriz.new_page()
        texto_matriz = efetuar_login_e_scraping(page_m, login_url, user_matriz, pass_matriz, url_matriz)
        print(f"Matriz extraída: {len(texto_matriz)} caracteres.")
        ctx_matriz.close()

        # FILIAL
        texto_filial = ""
        if user_filial and pass_filial:
            print("--- PROCESSANDO FILIAL ---")
            ctx_filial = browser.new_context(viewport={"width": 1920, "height": 1080})
            page_f = ctx_filial.new_page()
            texto_filial = efetuar_login_e_scraping(page_f, login_url, user_filial, pass_filial, url_filial)
            print(f"Filial extraída: {len(texto_filial)} caracteres.")
            ctx_filial.close()

        browser.close()

    return f"""
    === DADOS UNIDADE MATRIZ ===
    {texto_matriz[:80000]}

    === DADOS UNIDADE FILIAL ===
    {texto_filial[:80000]}
    """

def processar_com_gemini(conteudo):
    prompt = f"""
    Você é um analisador de dados escolares do CFIS/CGD.
    Analise o texto bruto extraído abaixo.

    REGRAS DE EXTRAÇÃO:
    1. Identifique TODOS os alunos listados na MATRIZ e na FILIAL.
    2. Na MATRIZ: Conte apenas alunos ativos.
    3. Na FILIAL: Conte alunos ativos listados.
    4. Alunos CRÍTICOS: Alunos com mais de 90 dias em curso.
    5. Alunos MODERADOS: Alunos entre 60 e 89 dias em curso.

    Responda APENAS um JSON válido e puro, sem blocos de texto ou marcadores markdown:
    {{
      "total_matriz": 0,
      "total_filial": 0,
      "alunos_criticos": 0,
      "alunos_moderados": 0,
      "detalhes": [
         {{"nome": "Nome do Aluno", "unidade": "Matriz/Filial", "laboratorio": "Lab", "status": "CRÍTICO/MODERADO/NORMAL", "dias": 0}}
      ]
    }}

    CONTEÚDO BRUTO:
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
    print("Atualização no Supabase finalizada!")

if __name__ == "__main__":
    print("Iniciando automação...")
    dados = efetuar_scraping_cgd()
    print("Analisando com Gemini...")
    resultado = processar_com_gemini(dados)
    print("Enviando ao Supabase...")
    salvar_no_supabase(resultado)
    print("Concluído!")
