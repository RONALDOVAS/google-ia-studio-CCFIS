import os
import json
import time
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

# Tenta carregar o stealth para mascarar o headless contra Cloudflare/WAF
try:
    from playwright_stealth import stealth_sync
    STEALTH_DISPONIVEL = True
except ImportError:
    STEALTH_DISPONIVEL = False

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def extrair_alunos_completos():
    with sync_playwright() as p:
        # Lança navegador simulando estritamente um Google Chrome real
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox"
            ]
        )
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="pt-BR"
        )
        page = context.new_page()

        # Aplica o stealth no contexto da página se disponível
        if STEALTH_DISPONIVEL:
            stealth_sync(page)

        login_url = os.environ.get("CGD_LOGIN_URL") or "https://app.cgd.com.br"
        relatorio_url = os.environ.get("CGD_MATRIZ_URL") or os.environ.get("URL_ALUNOS_MATRIZ") or login_url
        usuario = (os.environ.get("CGD_USER_MATRIZ") or os.environ.get("CGD_USER") or "").strip()
        senha = (os.environ.get("CGD_PASS_MATRIZ") or os.environ.get("CGD_PASS") or "").strip()

        print(f"1. Acessando URL de Login: {login_url}")
        page.goto(login_url, wait_until="networkidle", timeout=30000)

        seletor_user = 'input[name="usuario"], input[name="login"], input[name="email"], input[id*="user"], input[id*="login"], input[type="text"]'
        seletor_pass = 'input[type="password"]'

        try:
            page.wait_for_selector(seletor_user, timeout=10000, state="visible")
            
            field_user = page.locator(seletor_user).first
            field_user.click()
            field_user.press_sequentially(usuario, delay=40)

            field_pass = page.locator(seletor_pass).first
            field_pass.click()
            field_pass.press_sequentially(senha, delay=40)
            
            page.wait_for_timeout(500)

            print("Enviando credenciais no formulário...")
            btn_login = page.locator('button[type="submit"], input[type="submit"], button:has-text("Entrar"), button:has-text("Acessar"), .btn-primary').first
            
            if btn_login.is_visible():
                btn_login.click()
            else:
                field_pass.press("Enter")

            page.wait_for_timeout(5000)
            page.wait_for_load_state("networkidle")
        except Exception as err:
            print(f"Aviso no login: {err}")

        # NAVEGAÇÃO PARA O RELATÓRIO DE ALUNOS
        if page.url != relatorio_url and relatorio_url != login_url:
            print(f"2. Navegando para o relatório final: {relatorio_url}")
            page.goto(relatorio_url, wait_until="networkidle", timeout=30000)
            page.wait_for_timeout(3000)

        # CAPTURA DA TABELA
        try:
            page.wait_for_selector("table", timeout=15000)
            print("Tabela capturada com sucesso!")
        except Exception:
            print(f"Falha de acesso. URL atual: {page.url}")
            print(f"Título da Página: {page.title()}")
            browser.close()
            return []

        # EXPANDIR EXIBIÇÃO DA PAGINAÇÃO
        select_length = page.query_selector('select[name*="length"], select[name*="table_length"]')
        todos_exibidos = False
        
        if select_length:
            for val in ['-1', '10000', '1000', '500']:
                try:
                    page.select_option('select[name*="length"], select[name*="table_length"]', val)
                    page.wait_for_timeout(2000)
                    todos_exibidos = True
                    break
                except:
                    continue

        # LEITURA E RETORNO DOS DADOS
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
                    unidade_detectada = "Matriz" if ("matriz" in turma_unidade.lower() or "central" in turma_unidade.lower()) else "Filial"

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

            if todos_exibidos or novos_nesta_pagina == 0:
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

    supabase.table("resumo_cgd").upsert(payload).execute()
    print(f"Sucesso! Registros gravados no Supabase: {total_registros} alunos.")

if __name__ == "__main__":
    atualizar_supabase()
