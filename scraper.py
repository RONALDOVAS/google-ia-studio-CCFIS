import os
import json
import re
from playwright.sync_api import sync_playwright
from google import genai
from supabase import create_client

gemini_client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def extrair_linhas_tabela(page, nome_unidade):
    """Extrai diretamente as linhas das tabelas do DOM para não truncar dados."""
    print(f"[{nome_unidade}] Aguardando carregamento da tabela...")
    try:
        page.wait_for_selector('table tbody tr', state="visible", timeout=15000)
    except Exception as e:
        print(f"[{nome_unidade}] Tabela não carregou a tempo: {e}")
        return []

    # Tenta selecionar 'Todos' ou o maior valor no Select de paginação
    try:
        selects = page.locator('select[name*="length"], select[name*="table"]').all()
        for sel in selects:
            if sel.is_visible():
                options = sel.locator("option").all_inner_texts()
                for opt in ["All", "Todos", "500", "100"]:
                    if any(opt.lower() in o.lower() for o in options):
                        sel.select_option(label=[o for o in options if opt.lower() in o.lower()][0])
                        page.wait_for_timeout(3000)
                        break
    except Exception as e:
        print(f"[{nome_unidade}] Erro ao mudar paginação: {e}")

    # Extrai o texto limpo de cada linha da tabela
    rows = page.locator('table tbody tr').all_inner_texts()
    print(f"[{nome_unidade}] Total de registros brutos capturados na página: {len(rows)}")
    return rows

def efetuar_login_e_extrair(browser, url_login, usuario, senha, nome_unidade):
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    try:
        print(f"[{nome_unidade}] Efetuando login...")
        page.goto(url_login, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        page.locator('#login-email, input[name="email"], input[type="email"]').first.fill(usuario)
        page.locator('input[name="password"], input[type="password"]').first.fill(senha)

        submit_btn = page.locator('button[type="submit"], input[type="submit"]').first
        if submit_btn.is_visible():
            submit_btn.click()
        else:
            page.keyboard.press("Enter")

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(3000)

        # Clica no menu 'Alunos'
        menu_alunos = page.locator('a:has-text("Alunos"), span:has-text("Alunos")').first
        if menu_alunos.is_visible():
            menu_alunos.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(3000)

        linhas = extrair_linhas_tabela(page, nome_unidade)
        return linhas

    except Exception as e:
        print(f"[{nome_unidade}] Erro na execução: {e}")
        return []
    finally:
        context.close()

def efetuar_scraping_cgd():
    login_url = os.environ.get("CGD_LOGIN_URL")
    user_matriz = os.environ.get("CGD_USER_MATRIZ")
    pass_matriz = os.environ.get("CGD_PASS_MATRIZ")
    user_filial = os.environ.get("CGD_USER_FILIAL")
    pass_filial = os.environ.get("CGD_PASS_FILIAL") or os.environ.get("CDG_PASS_FILIAL")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])

        linhas_matriz = efetuar_login_e_extrair(browser, login_url, user_matriz, pass_matriz, "Matriz")
        linhas_filial = []
        if user_filial and pass_filial:
            linhas_filial = efetuar_login_e_extrair(browser, login_url, user_filial, pass_filial, "Filial")

        browser.close()

    return {
        "matriz": linhas_matriz,
        "filial": linhas_filial
    }

def processar_com_gemini(dados_brutos):
    prompt = f"""
    Você é o assistente de dados do sistema escolar CFIS/CGD.
    Abaixo estão as linhas extraídas diretamente da tabela de alunos.

    Sua tarefa é estruturar TODOS os alunos fornecidos no JSON de saída sem omitir nenhum nome.

    REGRAS:
    1. Trate cada linha enviada como um aluno.
    2. Identifique o Nome, Contrato/Matrícula, Curso e Dias de Curso/Último Acesso.
    3. Classifique o status de criticidade:
       - CRÍTICO: dias > 90
       - MODERADO: dias entre 60 e 89
       - ATENÇÃO: dias entre 30 e 59
       - NORMAL: dias < 30
    4. Atribua 'unidade': 'Matriz' para os alunos da Matriz e 'unidade': 'Filial' para os da Filial.

    Responda EXCLUSIVAMENTE um JSON neste formato sem sintaxe markdown:
    {{
      "total_matriz": {len(dados_brutos['matriz'])},
      "total_filial": {len(dados_brutos['filial'])},
      "alunos_criticos": 0,
      "alunos_moderados": 0,
      "detalhes": [
         {{
           "contrato": "000",
           "nome": "NOME DO ALUNO",
           "unidade": "Matriz ou Filial",
           "curso": "Nome do Curso",
           "status": "NORMAL/CRÍTICO/MODERADO/ATENÇÃO",
           "dias": 0,
           "faltas": 0
         }}
      ]
    }}

    LINHAS DA MATRIZ:
    {json.dumps(dados_brutos['matriz'][:300], ensure_ascii=False)}

    LINHAS DA FILIAL:
    {json.dumps(dados_brutos['filial'][:300], ensure_ascii=False)}
    """
    
    response = gemini_client.models.generate_content(
        model='gemini-3.6-flash',
        contents=prompt
    )
    return response.text

def salvar_no_supabase(resultado_ia):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Supabase não configurado.")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    try:
        match = re.search(r'\{.*\}', resultado_ia, re.DOTALL)
        dados_json = json.loads(match.group(0)) if match else json.loads(resultado_ia)
    except Exception as e:
        print(f"Erro no parse JSON: {e}")
        dados_json = {}

    detalhes = dados_json.get("detalhes", [])
    criticos = sum(1 for a in detalhes if a.get("status") == "CRÍTICO")
    moderados = sum(1 for a in detalhes if a.get("status") == "MODERADO")

    payload = {
        "id": 1,
        "relatorio": resultado_ia,
        "total_filial": dados_json.get("total_filial", 0),
        "total_matriz": dados_json.get("total_matriz", 0),
        "alunos_criticos": criticos,
        "alunos_moderados": moderados,
        "dados_completos": detalhes,
        "atualizado_em": "now()"
    }

    supabase.table("resumo_cgd").upsert(payload).execute()
    print("Sucesso: Supabase atualizado com dados reais e separados!")

if __name__ == "__main__":
    print("Iniciando extração direta de tabelas...")
    dados = efetuar_scraping_cgd()
    print("Processando linhas com Gemini...")
    resultado = processar_com_gemini(dados)
    print("Gravando no banco de dados...")
    salvar_no_supabase(resultado)
    print("Concluído!")
