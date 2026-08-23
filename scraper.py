import os
import json
import time
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

# Configurações do Supabase
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def extrair_alunos_completos():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()

        # 1. Login no Portal CGD
        page.goto("https://seu-portal-cgd.com/login") # Ajuste a URL de login
        page.fill('input[name="username"]', os.environ.get("CGD_USER"))
        page.fill('input[name="password"]', os.environ.get("CGD_PASS"))
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # 2. Navegar para a página do relatório de alunos
        page.goto("https://seu-portal-cgd.com/relatorios/alunos") # Ajuste a URL do relatório
        page.wait_for_selector("table", timeout=15000)

        # 3. FORÇAR EXIBIÇÃO DE TODOS OS REGISTROS (Tentar seletores de 500 / 10000 / Todos)
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
                    
                    # Evita duplicatas caso a paginação repita itens
                    chave_unica = f"{contrato_cod}_{nome_aluno}"
                    if chave_unica in contratos_processados:
                        continue
                    
                    contratos_processados.add(chave_unica)
                    novos_nesta_pagina += 1

                    turma_unidade = cols[2].inner_text().strip()
                    
                    # Detecta a unidade (Matriz ou Filial)
                    unidade_detectada = "Filial"
                    if "matriz" in turma_unidade.lower() or "central" in turma_unidade.lower():
                        unidade_detectada = "Matriz"

                    aluno = {
                        "contrato": nome_aluno,       # Nome completo do Aluno
                        "nome": contrato_cod,          # Matrícula/Código do Contrato
                        "curso": turma_unidade,        # Nome do Curso/Turma
                        "unidade": unidade_detectada,  # Matriz ou Filial
                        "status": "NORMAL",
                        "dias": int(cols[3].inner_text().strip()) if cols[3].inner_text().strip().isdigit() else 0,
                        "faltas": 0
                    }
                    todos_alunos.append(aluno)

            # Se todos já foram exibidos de uma vez ou não houve novos registros, encerra
            if todos_exibidos_de_uma_vez or novos_nesta_pagina == 0:
                break

            # Avança paginação caso a exibição 'Todos' não tenha funcionado
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

    # Separação e Contagem exatas por Unidade
    total_matriz = len([a for a in alunos if a.get("unidade") == "Matriz"])
    total_filial = len([a for a in alunos if a.get("unidade") == "Filial"])

    # Payload formatado diretamente para colunas JSONB
    payload = {
        "id": 1, # Atualiza sempre a linha de ID 1
        "total_filial": total_filial,
        "total_matriz": total_matriz,
        "dados_completos": alunos,  # Envia como Lista Python (O Supabase converte p/ JSONB nativo)
        "alunos_criticos": len([a for a in alunos if a.get("dias", 0) >= 90]),
        "alunos_moderados": len([a for a in alunos if 60 <= a.get("dias", 0) < 90]),
        "atualizado_em": time.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    }

    # Upsert (Atualiza a linha existente sem criar duplicatas)
    response = supabase.table("resumo_cgd").upsert(payload).execute()
    print(f"Sucesso! Atualizado registro no Supabase com {total_registros} alunos (Matriz: {total_matriz}, Filial: {total_filial}).")

if __name__ == "__main__":
    atualizar_supabase()
