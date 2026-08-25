import os
import json
import time
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

# 1. Leitura das credenciais do GitHub Secrets
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

LOGIN_MATRIZ = os.getenv("CGD_USER_MATRIZ")
SENHA_MATRIZ = os.getenv("CGD_PASS_MATRIZ")
URL_MATRIZ = os.getenv("URL_ALUNOS_MATRIZ") or os.getenv("CGD_MATRIZ_URL")

LOGIN_FILIAL = os.getenv("CGD_USER_FILIAL")
SENHA_FILIAL = os.getenv("CGD_PASS_FILIAL")
URL_FILIAL = os.getenv("URL_ALUNOS_FILIAL") or os.getenv("CGD_FILIAL_URL")

CGD_URL = os.getenv("CGD_LOGIN_URL") or "https://cgdgestao.com.br"


def fazer_login_e_extrair(page, usuario, senha, url_destino, unidade_nome):
    print(f"--- Iniciando processamento: {unidade_nome} ---")
    alunos_capturados = []
    
    try:
        # Acessa a página principal de login
        page.goto(CGD_URL, wait_until="networkidle", timeout=60000)
        page.wait_for_timeout(2000)

        # Seletores flexíveis de campo de login e senha
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
            print(f"Aviso: Campo de login não visível para {unidade_nome}. Verificando se já está autenticado...")

        # Se houver uma URL direta para a listagem/tabela de alunos da unidade, navega até ela
        if url_destino and url_destino != CGD_URL:
            page.goto(url_destino, wait_until="networkidle", timeout=60000)

        page.wait_for_timeout(3000)

        # Tenta expandir para exibir 'Todos' ou o máximo de registros na paginação
        try:
            select_limit = page.locator('select[name*="length"], select[name*="limit"], select[name*="per_page"]').first
            if select_limit.is_visible(timeout=3000):
                select_limit.select_option(value="-1")
                page.wait_for_timeout(2000)
        except Exception:
            pass

        # Extração de linhas da tabela
        rows = page.locator('table tbody tr').all()
        print(f"Total de linhas encontradas na tabela de {unidade_nome}: {len(rows)}")

        for row in rows:
            cols = row.locator('td').all_text_contents()
            if cols and len(cols) >= 2:
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
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Erro crítico: SUPABASE_URL ou SUPABASE_SERVICE_ROLE_KEY não foram encontradas no ambiente.")
        return

    todos_alunos = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(viewport={'width': 1280, 'height': 800})
        page = context.new_page()

        # Processar Matriz
        if LOGIN_MATRIZ and SENHA_MATRIZ:
            alunos_matriz = fazer_login_e_extrair(page, LOGIN_MATRIZ, SENHA_MATRIZ, URL_MATRIZ, "Matriz")
            todos_alunos.extend(alunos_matriz)
        else:
            print("Aviso: Credenciais da Matriz não encontradas nos Secrets.")

        # Limpar sessão para a Filial
        context.clear_cookies()

        # Processar Filial
        if LOGIN_FILIAL and SENHA_FILIAL:
            alunos_filial = fazer_login_e_extrair(page, LOGIN_FILIAL, SENHA_FILIAL, URL_FILIAL, "Filial")
            todos_alunos.extend(alunos_filial)
        else:
            print("Aviso: Credenciais da Filial não encontradas nos Secrets.")

        browser.close()

    print(f"--- Fim da raspagem. Total geral de alunos capturados: {len(todos_alunos)} ---")

    # Atualização no Supabase
    if todos_alunos:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        data = {
            "relatorio": json.dumps(todos_alunos, ensure_ascii=False),
            "atualizado_em": "now()"
        }
        res = supabase.table("resumo_cgd").upsert(data).execute()
        print("Relatório de alunos enviado com sucesso para a tabela resumo_cgd no Supabase!")
    else:
        print("Nenhum aluno foi capturado para atualização.")


if __name__ == "__main__":
    main()
