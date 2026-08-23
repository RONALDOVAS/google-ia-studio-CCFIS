import os
import json
import time
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

# Configurações do Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def extrair_alunos_completos():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
        page = context.new_page()

        # 1. Tentar acessar a URL principal
        login_url = os.environ.get("CGD_LOGIN_URL") or os.environ.get("CGD_MATRIZ_URL")
        print(f"Acessando URL: {login_url}")
        page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
        page.wait_for_timeout(3000)

        usuario = os.environ.get("CGD_USER_MATRIZ") or os.environ.get("CGD_USER")
        senha = os.environ.get("CGD_PASS_MATRIZ") or os.environ.get("CGD_PASS")

        # Verifica se há um formulário de login visível
        input_usuario = page.query_selector('input[type="text"], input[type="email"], input[name*="user"], input[name*="login"], input[id*="user"]')
        input_senha = page.query_selector('input[type="password"]')

        if input_usuario and input_senha:
            print("Formulário de login detectado. Realizando autenticação...")
            input_usuario.fill(usuario)
            input_senha.fill(senha)
            
            # Submete o formulário via tecla ENTER (evita falhas de botões invisíveis)
            input_senha.press("Enter")
            page.wait_for_timeout(5000)
        else:
            print("Nenhum formulário de login explícito encontrado. Prosseguindo...")

        # 2. Navegar para a página do relatório de alunos
        relatorio_url = os.environ.get("URL_ALUNOS_MATRIZ") or login_url
        if page.url != relatorio_url:
            print(f"Navegando para a página de dados: {relatorio_url}")
            page.goto(relatorio_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(3000)

        # Aguarda a tabela carregar na tela
        try:
            page.wait_for_selector("table", timeout=15000)
            print("Tabela encontrada com sucesso!")
        except Exception as e:
            print(f"Erro ao localizar a tabela. URL atual: {page.url}")
            print(f"Conteúdo do Título da Página: {page.title()}")
            browser.close()
            return []

        # 3. FORÇAR EXIBIÇÃO DE TODOS OS REGISTROS
        select_length = page.query_selector('select[name*="length"], select[name*="table_length"]')
        todos_exibidos_de_uma_vez = False
        
        if select_length:
            for val in ['-1', '10000', '1000', '500']:
                try:
                    page.select_option('select[name*="length"], select[name*="table_length"]', val)
                    page.wait_for_timeout(2000)
                    todos_exibidos_de_uma_vez = True
                    break
                except:
                    continue

        # 4. Extração de registros com verificação contra duplicados
        todos_alunos = []
        contratos_processados = set()

        while True:
            rows = page.query_selector_all("table tbody tr")
            novos_nesta_pagina = 0

            for row in rows:
                cols = row.query_selector_all("td")
                if len(cols) >= 4:
                    contrato_cod = cols[0].inner_text().strip()
                    nome_aluno = cols[1].inner_text().strip()
                    
                    chave_unica = f"{contrato_cod}_{nome_aluno}"
                    if chave_unica in contratos_processados:
                        continue
                    
                    contratos_processados.add(chave_unica)
                    novos_nesta_pagina += 1

                    turma_unidade = cols[2].inner_text().strip()
                    
                    unidade_detectada = "Filial"
                    if "matriz" in turma_unidade.lower() or "central" in turma_unidade.lower():
                        unidade_detectada = "Matriz"

                    aluno = {
                        "contrato": nome_aluno,       
                        "nome": contrato_cod,          
                        "curso": turma_unidade,        
                        "unidade": unidade_detectada,  
                        "status": "NORMAL",
                        "dias": int(cols[3].inner_text().strip()) if cols[3].inner_text().strip().isdigit() else 0,
                        "faltas": 0
                    }
                    todos_alunos.append(aluno)

            if todos_exibidos_de_uma_vez or novos_nesta_pagina == 0:
                break

            next_btn = page.query_selector('.paginate_button.next:not(.disabled), a[rel="next"]:not(.disabled)')
            if next_btn and next_btn.is_visible():
                next_btn.click()
                page.wait_for_timeout(1500)
            else:
                break

        browser.close()
        return todos_alunos

def atualizar_supabase():
    alunos = extrair_alunos_completos()
    total_registros = len(alunos)

    if total_registros == 0:
        print("Aviso: Nenhum aluno capturado pelo scraper.")
        return

    total_matriz = len([a for a in alunos if a.get("unidade") == "Matriz"])
    total_filial = len([a for a in alunos if a.get("unidade") == "Filial"])

    payload = {
        "id": 1,
        "total_filial": total_filial,
        "total_matriz": total_matriz,
        "dados_completos": alunos,
        "alunos_criticos": len([a for a in alunos if a.get("dias", 0) >= 90]),
        "alunos_moderados": len([a for a in alunos if 60 <= a.get("dias", 0) < 90]),
        "atualizado_em": time.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    }

    response = supabase.table("resumo_cgd").upsert(payload).execute()
    print(f"Sucesso! Atualizado registro no Supabase com {total_registros} alunos (Matriz: {total_matriz}, Filial: {total_filial}).")

if __name__ == "__main__":
    atualizar_supabase()
