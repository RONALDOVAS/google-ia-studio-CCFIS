import os
import json
import requests
from bs4 import BeautifulSoup
import google.generativeai as genai

# Configuração da API do Gemini
genai.configure(api_key=os.environ.get("GEMINI_API_KEY"))

def efetuar_scraping_cgd():
    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
    })
    
    login_url = os.environ.get("CGD_LOGIN_URL") or "https://seucgd.com.br/login"
    url_matriz = os.environ.get("CGD_MATRIZ_URL") or "https://seucgd.com.br/matriz/alunos"
    url_filial = os.environ.get("CGD_FILIAL_URL") or "https://seucgd.com.br/filial/alunos"
    
    # 1. Acessa a página de login para obter cookies e token CSRF
    res_login_page = session.get(login_url)
    soup_login = BeautifulSoup(res_login_page.text, 'html.parser')
    
    # Busca o token no formulário (_token) ou nas meta tags
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

    # 2. Faz o POST do login
    response = session.post(login_url, data=login_payload)
    response.raise_for_status()
    
    # 3. Raspa as páginas de alunos
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
    Você é um assistente de gestão escolar do CGD.
    Analise os dados extraídos das páginas da Matriz e da Filial abaixo.
    
    Retorne a análise em formato de texto estruturado contendo a lista de alunos e vinculações.
    
    Dados Brutos:
    {conteudo}
    """
    
    model = genai.GenerativeModel('gemini-1.5-flash')
    response = model.generate_content(prompt)
    return response.text

if __name__ == "__main__":
    print("Iniciando scraping do CGD...")
    dados = efetuar_scraping_cgd()
    
    print("Processando dados com o Gemini...")
    resultado = processar_com_gemini(dados)
    
    dados_finais = {
        "status": "sucesso",
        "relatorio": resultado
    }
    
    with open("dados_alunos.json", "w", encoding="utf-8") as f:
        json.dump(dados_finais, f, ensure_ascii=False, indent=4)
        
    print("Arquivo dados_alunos.json gerado com sucesso!")
