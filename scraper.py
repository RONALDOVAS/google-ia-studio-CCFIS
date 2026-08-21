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
    cgd_user = os.environ.get("CGD_USER")
    cgd_pass = os.environ.get("CGD_PASS")

    if not all([login_url, url_matriz, url_filial, cgd_user, cgd_pass]):
        raise ValueError("Variáveis do CGD ausentes nos Secrets.")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # 1. Login no portal
        page.goto(login_url)
        page.wait_for_selector('input[name="usuario"]')
        page.fill('input[name="usuario"]', cgd_user)
        page.fill('input[name="senha"]', cgd_pass)
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # 2. Raspagem Matriz
        page.goto(url_matriz)
        page.wait_for_load_state("networkidle")
        texto_matriz = page.inner_text("body")

        # 3. Raspagem Filial
        page.goto(url_filial)
        page.wait_for_load_state("networkidle")
        texto_filial = page.inner_text("body")

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
