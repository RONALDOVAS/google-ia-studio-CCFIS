import os
import requests
from bs4 import BeautifulSoup
from google import genai

# Inicializa o cliente Gemini usando a chave vinda dos Secrets
client = genai.Client(api_key=os.environ.get("GEMINI_API_KEY"))

def efetuar_scraping_cgd():
    session = requests.Session()
    
    login_url = os.environ.get("CGD_LOGIN_URL", "https://seucgd.com.br/login")
    url_matriz = os.environ.get("CGD_MATRIZ_URL", "https://seucgd.com.br/matriz/alunos")
    url_filial = os.environ.get("CGD_FILIAL_URL", "https://seucgd.com.br/filial/alunos")
    
    login_payload = {
        'usuario': os.environ.get("CGD_USER"),
        'senha': os.environ.get("CGD_PASS")
    }
    
    # Realiza o login na plataforma
    response = session.post(login_url, data=login_payload)
    response.raise_for_status()
    
    # Busca os dados da Matriz e Filial
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
    
    Tarefas:
    1. Identifique todos os alunos cadastrados na Matriz e na Filial.
    2. Faça a correspondência/vinculação entre alunos que possuem cadastro na Filial e na Matriz (por CPF, ID ou Nome).
    3. Retorne uma lista estruturada contendo:
       - Nome do Aluno
       - Matrícula/ID
       - Unidade de Origem
       - Status de Vinculação entre Matriz e Filial
    
    Dados Brutos:
    {conteudo}
    """
    
    response = client.models.generate_content(
        model='gemini-2.5-flash',
        contents=prompt
    )
    return response.text

if __name__ == "__main__":
    print("Iniciando scraping do CGD...")
    dados = efetuar_scraping_cgd()
    print("Processando dados com o Gemini...")
    resultado = processar_com_gemini(dados)
    
    print("\n--- RESULTADO PROCESSADO ---")
    print(resultado)
    
    with open("resultado_alunos.txt", "w", encoding="utf-8") as f:
        f.write(resultado)
