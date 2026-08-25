import os
import json
import time
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

# Configurações do Supabase (lidas dos Secrets do GitHub ou variáveis de ambiente)
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# Credenciais do CGD
LOGIN_MATRIZ = os.getenv("LOGIN_MATRIZ")
SENHA_MATRIZ = os.getenv("SENHA_MATRIZ")
LOGIN_FILIAL = os.getenv("LOGIN_FILIAL")
SENHA_FILIAL = os.getenv("SENHA_FILIAL")
CGD_URL = os.getenv("CGD_URL", "https://cgdgestao.com.br") # Ajuste se a URL inicial for outra

def fazer_login_e_extrair(page, usuario, senha, unidade_nome):
    print(f"--- Iniciando processamento: {unidade_nome} ---")
    alunos_capturados = []
    
    try:
        # Acesse a página e aguarde até a rede ficar ociosa
        page.goto(CGD_URL, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(3000)

        # Procura seletores genéricos e flexíveis de login
        login_input = page.locator('input[type="text"], input[type="email"], input[name*="user"], input[name*="login"], input[id*="user"]').first
        senha_input = page.locator('input[type="password"]').first
        submit_btn = page.locator('button[type="submit"], input[type="submit"], button:has-text("Entrar"), button:has-text("Acessar"), button:has-text("Login")').first

        if login_input.is_visible(timeout=15000):
            login_input.fill(usuario)
            senha_input.fill(senha)
            submit_btn.click()
            page.wait_for_load_state("networkidle", timeout=60000)
            print(f"Login efetuado com sucesso para {unidade_nome}.")
        else:
            print(f"Aviso: Campo de login não visível para {unidade_nome}. Verifique se já está autenticado ou se a página mudou.")

        # Aguarda o carregamento das tabelas/dados dos alunos
        page.wait_for_timeout(5000)

        # Se houver seletor de paginação/exibição para 'Mostrar Todos' ou '100 por página', tenta selecionar
        try:
            select_limit = page.locator('select[name*="length"], select[name*="limit"], select[name*="per_page"]').first
            if select_limit.is_visible(timeout=5000):
                select_limit.select_option(value="-1") # Tenta "Todos" ou o maior valor
                page.wait_for_timeout(3000)
        except Exception:
            pass

        # Extração genérica de linhas da tabela de alunos
        rows = page.locator('table tbody tr').all()
        print(f"Total de linhas encontradas na tabela de {unidade_nome}: {len(rows)}")

        for row in rows:
            cols = row.locator('td').all_text_contents()
            if cols and len(cols) >= 2:
                # Mapeie os campos conforme a estrutura exata do seu CGD
                aluno_data = {
                    "contrato": cols[0].strip() if len(cols) > 0 else "",
                    "nome": cols[1].strip() if len(cols) > 1 else "",
                    "unidade": unidade_nome,
                    "curso": cols[2].strip() if len(cols) > 2 else "",
                    "status": cols[3].strip() if len(cols) > 3 else "NORMAL",
                    "dias": 0,
                    "faltas": 0
                }
                alunos_capturados.append(aluno_data)

    except Exception as e:
        print(f"Erro ao processar {unidade_nome}: {str(e)}")

    return alunos_capturados


def main():
    todos_alunos = []

    with sync_playwright() as p:
        # Abre o navegador em modo headless
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = context.new_page()

        # 1. Capturar Matriz
        if LOGIN_MATRIZ and SENHA_MATRIZ:
            alunos_matriz = fazer_login_e_extrair(page, LOGIN_MATRIZ, SENHA_MATRIZ, "Matriz")
            todos_alunos.extend(alunos_matriz)
        
        # Limpar cookies/sessão para a Filial
        context.clear_cookies()
        
        # 2. Capturar Filial
        if LOGIN_FILIAL and SENHA_FILIAL:
            alunos_filial = fazer_login_e_extrair(page, LOGIN_FILIAL, SENHA_FILIAL, "Filial")
            todos_alunos.extend(alunos_filial)

        browser.close()

    print(f"--- Fim da raspagem. Total de alunos capturados: {len(todos_alunos)} ---")

    # 3. Salvar/Atualizar no Supabase
    if SUPABASE_URL and SUPABASE_KEY:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        
        # Atualiza a tabela resumo_cgd com o relatório completo dos alunos em formato JSON
        data = {
            "relatorio": json.dumps(todos_alunos, ensure_ascii=False),
            "atualizado_em": "now()"
        }
        
        # Executa o upsert/update no banco
        res = supabase.table("resumo_cgd").upsert(data).execute()
        print("Dados enviados com sucesso para o Supabase!")
    else:
        print("Erro: Variáveis SUPABASE_URL ou SUPABASE_KEY não foram encontradas.")

if __name__ == "__main__":
    main()
