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
        page.wait_for_selector("table")

        # 3. FORÇAR EXIBIÇÃO DE TODOS OS REGISTROS (Mudar de 10 para 'Todos' / -1)
        select_length = page.query_selector('select[name*="length"]')
        if select_length:
            try:
                page.select_option('select[name*="length"]', '-1')
            except:
                try:
                    page.select_option('select[name*="length"]', '10000')
                except:
                    pass
            page.wait_for_timeout(3000)

        # 4. Extração com Varredura de Paginação (Garante 100% dos registros)
        todos_alunos = []
        
        while True:
            # Captura todas as linhas visíveis da tabela
            rows = page.query_selector_all("table tbody tr")
            for row in rows:
                cols = row.query_selector_all("td")
                if len(cols) >= 4:
                    aluno = {
                        "contrato": cols[0].inner_text().strip(),
                        "aluno": cols[1].inner_text().strip(),
                        "turma": cols[2].inner_text().strip(),
                        "dias": int(cols[3].inner_text().strip()) if cols[3].inner_text().strip().isdigit() else 0,
                        "ultimo_acesso": cols[4].inner_text().strip() if len(cols) > 4 else ""
                    }
                    todos_alunos.append(aluno)

            # Verifica se há próxima página ativada
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
    
    # Separação/Contagem por Filial e Matriz
    alunos_filial = [a for a in alunos if "Filial" in a.get("turma", "") or "Castanhal" in a.get("turma", "")]
    alunos_matriz = [a for a in alunos if "Matriz" in a.get("turma", "") or "Central" in a.get("turma", "")]

    total_filial = len(alunos_filial) if alunos_filial else total_registros
    total_matriz = len(alunos_matriz) if alunos_matriz else 0

    # Payload completo sem limite de 10
    payload = {
        "total_filial": total_filial,
        "total_matriz": total_matriz,
        "dados_completos": json.dumps(alunos),
        "alunos_criticos": len([a for a in alunos if a.get("dias", 0) > 90]),
        "alunos_moderados": len([a for a in alunos if 60 <= a.get("dias", 0) <= 89])
    }

    # Gravação na tabela resumo_cgd
    response = supabase.table("resumo_cgd").insert(payload).execute()
    print(f"Sucesso! Enviados {total_registros} alunos para a tabela resumo_cgd.")

if __name__ == "__main__":
    atualizar_supabase()
