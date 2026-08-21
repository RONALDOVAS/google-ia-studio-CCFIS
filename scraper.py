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
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36'
    })
    
    login_url = os.environ.get("CGD_LOGIN_URL") or "https://seucgd.com.br/login"
    url_matriz = os.environ.get("CGD_MATRIZ_URL") or "https://seucgd.com.br/matriz/alunos"
    url_filial = os.environ.get("CGD_FILIAL_URL") or "https://seucgd.com.br/filial/alunos"
    
    login_payload = {
        'usuario': os.environ.get("CGD_USER"),
        'senha': os.environ.get("CGD_PASS")
    }
    
    response = session.post(login_url, data=login_payload)
    response.raise_for_status()
    
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
    
    # Estrutura tratada para salvar em JSON
    dados_finais = {
        "status": "sucesso",
        "relatorio": resultado
    }
    
    with open("dados_alunos.json", "w", encoding="utf-8") as f:
        json.dump(dados_finais, f, ensure_ascii=False, indent=4)
        
    print("Arquivo dados_alunos.json gerado com sucesso!")
