import os
import json
import re
from playwright.sync_api import sync_playwright
from google import genai
from supabase import create_client

gemini_client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def navegar_e_extrair_alunos(page):
    # Clica no menu "Alunos" do painel lateral
    print("Navegando via menu lateral 'Alunos'...")
    menu_alunos = page.locator('a:has-text("Alunos"), span:has-text("Alunos")').first
    
    if menu_alunos.is_visible():
        menu_alunos.click()
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(4000)
    else:
        print("Menu 'Alunos' não encontrado de forma visível.")

    # Aguarda a tabela ou lista carregar
    try:
        page.wait_for_selector('table, .dataTables_wrapper, .table-responsive', state="visible", timeout=20000)
    except Exception as e:
        print(f"Aviso ao esperar tabela: {e}")

    # Tenta alterar a paginação do DataTables/Select para exibir todos os registros
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
    return texto

def efetuar_login_e_extrair(browser, url_login, usuario, senha, nome_unidade):
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    try:
        print(f"[{nome_unidade}] Efetuando login em {url_login}...")
        page.goto(url_login, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(3000)

        user_input = page.locator('#login-email, input[name="email"], input[type="email"]').first
        pass_input = page.locator('input[name="password"], input[type="password"]').first

        user_input.fill(usuario)
        pass_input.fill(senha)

        submit_btn = page.locator('button[type="submit"], input[type="submit"]').first
        if submit_btn.is_visible():
            submit_btn.click()
        else:
            pass_input.press("Enter")

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(4000)

        # Navega navegando pela interface em vez de usar URL direta
        texto = navegar_e_extrair_alunos(page)
        print(f"[{nome_unidade}] Sucesso: {len(texto)} caracteres extraídos.")
        return texto

    except Exception as e:
        print(f"[{nome_unidade}] Erro durante execução: {e}")
        return ""
    finally:
        context.close()

def efetuar_scraping_cgd():
    login_url = os.environ.get("CGD_LOGIN_URL")
    user_matriz = os.environ.get("CGD_USER_MATRIZ")
    pass_matriz = os.environ.get("CGD_PASS_MATRIZ")
    
    user_filial = os.environ.get("CGD_USER_FILIAL")
    pass_filial = os.environ.get("CGD_PASS_FILIAL") or os.environ.get("CDG_PASS_FILIAL")

    if not login_url or not user_matriz or not pass_matriz:
        raise ValueError("Credenciais de login ausentes nos Secrets.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"])

        texto_matriz = efetuar_login_e_extrair(browser, login_url, user_matriz, pass_matriz, "Matriz")
        
        texto_filial = ""
        if user_filial and pass_filial:
            texto_filial = efetuar_login_e_extrair(browser, login_url, user_filial, pass_filial, "Filial")

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
    print("Atualização concluída no Supabase com sucesso.")

if __name__ == "__main__":
    print("Iniciando scraping...")
    dados = efetuar_scraping_cgd()
    print("Processando com Gemini...")
    resultado = processar_com_gemini(dados)
    print("Atualizando banco de dados...")
    salvar_no_supabase(resultado)
    print("Finalizado!")
