import os
import json
import re
from playwright.sync_api import sync_playwright
from google import genai
from supabase import create_client

gemini_client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def capturar_debug_e_extrair(browser, url_login, usuario, senha, url_alvo, nome_unidade):
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    try:
        print(f"[{nome_unidade}] Acessando URL de login...")
        page.goto(url_login, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(4000)

        # Salva o screenshot inicial para depuração
        page.screenshot(path=f"debug_login_{nome_unidade.lower()}.png")
        print(f"[{nome_unidade}] Screenshot da página inicial salvo.")

        # Imprime todos os inputs encontrados na página
        inputs = page.locator("input").all()
        print(f"[{nome_unidade}] Total de inputs encontrados: {len(inputs)}")
        for idx, inp in enumerate(inputs):
            try:
                print(f"  Input {idx}: type='{inp.get_attribute('type')}', name='{inp.get_attribute('name')}', id='{inp.get_attribute('id')}', visible={inp.is_visible()}")
            except Exception:
                pass

        # Tenta preencher formulário
        user_input = page.locator('input[type="text"], input[type="email"], input[name*="user"], input[name*="login"], input[name*="email"], input[id*="user"]').first
        pass_input = page.locator('input[type="password"], input[name*="senha"], input[name*="pass"], input[id*="pass"]').first

        if user_input.is_visible() and pass_input.is_visible():
            user_input.fill(usuario)
            pass_input.fill(senha)
            print(f"[{nome_unidade}] Campos preenchidos.")
            
            submit_btn = page.locator('button[type="submit"], input[type="submit"], button:has-text("Entrar"), button:has-text("Acessar")').first
            if submit_btn.is_visible():
                submit_btn.click()
            else:
                pass_input.press("Enter")
            
            page.wait_for_timeout(5000)
        else:
            print(f"[{nome_unidade}] ERRO: Campos de login/senha não encontrados ou invisíveis.")

        # Se houver URL específica da matriz/filial, acessa
        if url_alvo and url_alvo.strip() and url_alvo != page.url:
            print(f"[{nome_unidade}] Navegando para URL alvo: {url_alvo}")
            page.goto(url_alvo, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)

        page.screenshot(path=f"debug_pos_login_{nome_unidade.lower()}.png")
        texto = page.inner_text("body")
        print(f"[{nome_unidade}] Texto extraído ({len(texto)} chars): {texto[:300]}...")
        return texto

    except Exception as e:
        print(f"[{nome_unidade}] Erro durante execução: {e}")
        return ""
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

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox", "--disable-gpu"])

        texto_matriz = capturar_debug_e_extrair(browser, login_url, user_matriz, pass_matriz, url_matriz, "Matriz")
        
        texto_filial = ""
        if user_filial and pass_filial:
            texto_filial = capturar_debug_e_extrair(browser, login_url, user_filial, pass_filial, url_filial, "Filial")

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
    print("Atualizado no Supabase.")

if __name__ == "__main__":
    print("Iniciando depuração e scraping...")
    dados = efetuar_scraping_cgd()
    print("Enviando ao Gemini...")
    resultado = processar_com_gemini(dados)
    print("Atualizando banco...")
    salvar_no_supabase(resultado)
    print("Fim!")
