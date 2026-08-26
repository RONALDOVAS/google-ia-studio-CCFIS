import os
import json
import time
from datetime import datetime
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


def calcular_criticidade_e_dias(data_inicio_str):
    """Calcula os dias desde o início da disciplina e define a criticidade"""
    if not data_inicio_str or data_inicio_str.strip() == "":
        return "NORMAL", 0
    
    try:
        # Pega a data atual (pode usar datetime.now() também)
        hoje = datetime(2026, 8, 25) 
        # Tenta converter a data no padrão brasileiro DD/MM/YYYY
        data_inicio = datetime.strptime(data_inicio_str.strip(), "%d/%m/%Y")
        dias = (hoje - data_inicio).days
        
        if dias > 90:
            return "CRÍTICO", dias
        elif dias > 60:
            return "MODERADO", dias
        elif dias > 30:
            return "ATENÇÃO", dias
        else:
            return "NORMAL", max(0, dias)
    except Exception:
        return "NORMAL", 0


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

        # Navega para a URL da listagem
        if url_destino and url_destino != CGD_URL:
            page.goto(url_destino, wait_until="networkidle", timeout=60000)

        page.wait_for_timeout(3000)

        # Tenta expandir limite por página
        try:
            select_limit = page.locator('select[name*="length"], select[name*="limit"], select[name*="per_page"]').first
            if select_limit.is_visible(timeout=3000):
                select_limit.select_option(value="-1")
                page.wait_for_timeout(2000)
        except Exception:
            pass

        # --- NOVA LÓGICA DE PAGINAÇÃO E FILTRAGEM ---
        pagina_atual = 1
        while True:
            print(f"Raspando página {pagina_atual} de {unidade_nome}...")
            page.wait_for_timeout(2000)
            
            rows = page.locator('table tbody tr').all()
            for row in rows:
                cols = row.locator('td').all_text_contents()
                if cols and len(cols) >= 3:
                    contrato = cols[0].strip()
                    nome = cols[1].strip()
                    curso_texto = cols[2].strip().lower()
                    status_texto = cols[3].strip().upper() if len(cols) > 3 else "ATIVO"
                    
                    # 1. REGRA: DESCARTAR ALUNOS DESATIVADOS
                    if "DESATIVADO" in status_texto or "ENCERRADO" in status_texto or "INATIVO" in status_texto:
                        continue
                    
                    # 2. REGRA: APENAS CORRELATOS DE INFORMÁTICA
                    if "informática" not in curso_texto and "informatica" not in curso_texto:
                        continue

                    # 3. REGRA DA DISCIPLINA E DATA (Puxando a data para a criticidade)
                    data_inicio_str = ""
                    if len(cols) > 4: # Se a data já estiver na tabela principal na coluna 5
                        data_inicio_str = cols[4].strip()
                        
                    # 4. APLICAÇÃO DO CÁLCULO DE CRITICIDADE E DIAS
                    criticidade, dias_ativos = calcular_criticidade_e_dias(data_inicio_str)

                    aluno_data = {
                        "contrato": contrato,
                        "nome": nome,
                        "unidade": unidade_nome,
                        "curso": cols[2].strip(),
                        "status": "ATIVO",
                        "dias": dias_ativos,
                        "faltas": 0,
                        "criticidade": criticidade
                    }
                    alunos_capturados.append(aluno_data)

            # Clica no botão de próxima página para capturar todos (superando o limite)
            btn_proximo = page.locator('button:has-text("Próximo"), a:has-text("Próxima"), .paginate_button.next, [aria-label="Next"]').first
            
            # Se o botão de próximo existe e não está desabilitado, clica e continua o loop
            if btn_proximo.is_visible() and not btn_proximo.evaluate('node => node.classList.contains("disabled")'):
                btn_proximo.click()
                pagina_atual += 1
            else:
                break # Acabaram as páginas, sai do loop

        print(f"Total de alunos válidos filtrados em {unidade_nome}: {len(alunos_capturados)}")

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

    print(f"--- Fim da raspagem. Total geral de alunos capturados para atualização: {len(todos_alunos)} ---")

    # Atualização no Supabase
    if todos_alunos:
        supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)
        data = {
            "relatorio": json.dumps(todos_alunos, ensure_ascii=False),
            "atualizado_em": "now()"
        }
        # Salva o array JSON já calculado na tabela resumo_cgd
        res = supabase.table("resumo_cgd").upsert(data).execute()
        print("Relatório de alunos enviado com sucesso para a tabela resumo_cgd no Supabase!")
    else:
        print("Nenhum aluno válido foi capturado para atualização.")


if __name__ == "__main__":
    main()
