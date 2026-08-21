import os
import json
import requests
from bs4 import BeautifulSoup
from google import genai
from supabase import create_client

# Configurações de Clientes API
gemini_client = genai.Client(api_key=os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY"))

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def efetuar_scraping_cgd():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    
    login_url = os.environ.get("CGD_LOGIN_URL")
    url_matriz = os.environ.get("CGD_MATRIZ_URL")
    url_filial = os.environ.get("CGD_FILIAL_URL")
    
    if not login_url or not url_matriz or not url_filial:
        raise ValueError("Variáveis de URL do CGD não encontradas nos Secrets.")

    # 1. Obtém a página de login e token CSRF
    res_login_page = session.get(login_url)
    soup_login = BeautifulSoup(res_login_page.text, 'html.parser')
    
    csrf_token = None
    token_input = soup_login.find('input', {'name': '_token'})
    if token_input:
        csrf_token = token_input.get('value')
    else:
        meta_token = soup_login.find('meta', {'name': 'csrf-token'})
        if meta_token:
            csrf_token = meta_token.get('content')

    login_payload = {
        'usuario': os.environ.get("CGD_USER"),
        'senha': os.environ.get("CGD_PASS")
    }
    if csrf_token:
        login_payload['_token'] = csrf_token

    # 2. Executa o Login
    response = session.post(login_url, data=login_payload)
    response.raise_for_status()
    
    # 3. Raspagem das páginas
    res_matriz = session.get(url_matriz)
    res_filial = session.get(url_filial)
    
    soup_matriz = BeautifulSoup(res_matriz.text, 'html.parser')
    soup_filial = BeautifulSoup(res_filial.text, 'html.parser')
    
    dados_brutos = f"""
    --- DADOS ALUNOS MATRIZ ---
    {soup_matriz.get_text(separator=' ', strip=True)}
    
    --- DADOS ALUNOS FILIAL ---
    {soup_filial.get_text(separator=' ', strip=True)}
    """
    return dados_brutos

def processar_com_gemini(conteudo):
    prompt = f"""
    Você é um assistente de gestão escolar do CFIS/CGD.
    Analise os dados extraídos das páginas do sistema CGD.

    REGRAS DE FILTRAGEM OBRIGATÓRIAS:
    1. Na MATRIZ: Considere APENAS alunos ATIVOS vinculados ao "Laboratório 1" ou "Laboratório 2".
    2. Na FILIAL: Considere os alunos ativos conforme a listagem de turmas da filial.

    Retorne a análise em um formato JSON estruturado com contagens e níveis de criticidade (CRÍTICO, MODERADO, NORMAL).

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
        print("Aviso: SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY não configuradas. Ignorando envio ao banco.")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    # Atualiza ou insere na tabela resumo_cgd
    supabase.table("resumo_cgd").upsert({
        "id": 1,
        "relatorio": resultado_ia,
        "atualizado_em": "now()"
    }).execute()
    print("Dados sincronizados no Supabase com sucesso!")

if __name__ == "__main__":
    print("Iniciando scraping do CGD...")
    dados = efetuar_scraping_cgd()
    
    print("Processando dados no Gemini...")
    resultado = processar_com_gemini(dados)
    
    print("Enviando dados para o Supabase...")
    salvar_no_supabase(resultado)
    
    # Salva também a cópia local no JSON
    dados_finais = {
        "status": "sucesso",
        "relatorio": resultado
    }
    with open("dados_alunos.json", "w", encoding="utf-8") as f:
        json.dump(dados_finais, f, ensure_ascii=False, indent=4)
        
    print("Processo concluído!")
