import json

# Após processar com o Gemini/scraping:
resultado_dados = {
    "status": "sucesso",
    "resultado": resultado
}

with open("dados_alunos.json", "w", encoding="utf-8") as f:
    json.dump(resultado_dados, f, ensure_ascii=False, indent=4)

def efetuar_scraping_cgd():
    session = requests.Session()
    
    # Adiciona User-Agent para evitar bloqueios e define URLs padrão caso o secret venha vazio
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
