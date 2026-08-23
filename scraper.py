import os
import json
import time
from playwright.sync_api import sync_playwright
from supabase import create_client, Client

try:
    from playwright_stealth import stealth_sync
    STEALTH_ATIVO = True
except ImportError:
    STEALTH_ATIVO = False

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

def classificar_aluno(dias_sem_frequencia, faltas=0):
    if dias_sem_frequencia >= 90 or faltas >= 10:
        return "CRITICO", "BLOQUEADO"
    elif 60 <= dias_sem_frequencia < 90 or 5 <= faltas < 10:
        return "MODERADO", "ATENCAO"
    else:
        return "LEVE", "REGULAR"

def extrair_alunos_unidade(page, url_relatorio, nome_unidade):
    if not url_relatorio:
        return []

    print(f"Acessando relatório de {nome_unidade}: {url_relatorio}")
    page.goto(url_relatorio, wait_until="networkidle", timeout=40000)
    page.wait_for_timeout(3000)

    try:
        page.wait_for_selector("table", timeout=15000)
    except Exception:
        print(f"Erro ao carregar a tabela em {nome_unidade}.")
        return []

    # Tenta expandir o tamanho da paginação no Datatables para o máximo
    select_length = page.query_selector('select[name*="length"], select[name*="table_length"]')
    todos_exibidos = False
    if select_length:
        for val in ['-1', '10000', '5000', '1000']:
            try:
                page.select_option('select[name*="length"], select[name*="table_length"]', val)
                page.wait_for_timeout(3000)
                todos_exibidos = True
                break
            except Exception:
                continue

    alunos_coletados = []
    chaves_processadas = set()

    while True:
        rows = page.query_selector_all("table tbody tr")
        novos_nesta_pagina = 0

        for row in rows:
            cols = row.query_selector_all("td")
            if len(cols) >= 4:
                col_texto_completo = row.inner_text().lower()

                # FILTRO 1: Apenas alunos ATIVOS e de INFORMÁTICA
                if "informática" not in col_texto_completo and "informatica" not in col_texto_completo:
                    continue
                
                # Se houver coluna explicita de status, valida ativo; senao checa no texto da linha
                if "inativo" in col_texto_completo or "cancelado" in col_texto_completo:
                    continue

                contrato_cod = cols[0].inner_text().strip()
                nome_aluno = cols[1].inner_text().strip()
                curso_turma = cols[2].inner_text().strip()

                chave_unica = f"{contrato_cod}_{nome_aluno}"
                if chave_unica in chaves_processadas:
                    continue

                chaves_processadas.add(chave_unica)
                novos_nesta_pagina += 1

                # Trata dias e faltas das colunas
                col_dias = cols[3].inner_text().strip()
                col_faltas = cols[4].inner_text().strip() if len(cols) >= 5 else "0"

                dias_sem_freq = int(col_dias) if col_dias.isdigit() else 0
                faltas = int(col_faltas) if col_faltas.isdigit() else 0

                criticidade, status_bloqueio = classificar_aluno(dias_sem_freq, faltas)

                aluno = {
                    "contrato": contrato_cod,
                    "nome": nome_aluno,
                    "curso": curso_turma,
                    "unidade": nome_unidade,
                    "dias": dias_sem_freq,
                    "faltas": faltas,
                    "criticidade": criticidade,
                    "status": status_bloqueio
                }
                alunos_coletados.append(aluno)

        if todos_exibidos or novos_nesta_pagina == 0:
            break

        # Paginação manual caso o select não exiba todos em uma única página
        next_btn = page.query_selector('.paginate_button.next:not(.disabled), a[rel="next"]:not(.disabled)')
        if next_btn and next_btn.is_visible():
            next_btn.click()
            page.wait_for_timeout(2000)
        else:
            break

    print(f"Total filtrado para {nome_unidade} (Informática + Ativos): {len(alunos_coletados)}")
    return alunos_coletados

def extrair_alunos_completos():
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage"
            ]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1366, "height": 768},
            locale="pt-BR"
        )
        page = context.new_page()

        if STEALTH_ATIVO:
            stealth_sync(page)

        login_url = (os.environ.get("CGD_LOGIN_URL") or "https://app.cgd.com.br").strip()
        matriz_url = (os.environ.get("CGD_MATRIZ_URL") or os.environ.get("URL_ALUNOS_MATRIZ") or "").strip()
        filial_url = (os.environ.get("CGD_FILIAL_URL") or "").strip()

        usuario_matriz = (os.environ.get("CGD_USER_MATRIZ") or os.environ.get("CGD_USER") or "").strip()
        senha_matriz = (os.environ.get("CGD_PASS_MATRIZ") or os.environ.get("CGD_PASS") or "").strip()

        print(f"1. Efetuando login em: {login_url}")
        page.goto(login_url, wait_until="domcontentloaded", timeout=30000)

        seletor_user = 'input[name="usuario"], input[name="login"], input[name="email"], input[type="text"]'
        seletor_pass = 'input[type="password"]'

        try:
            page.wait_for_selector(seletor_user, timeout=10000, state="visible")
            page.locator(seletor_user).first.fill(usuario_matriz)
            page.locator(seletor_pass).first.fill(senha_matriz)
            page.wait_for_timeout(500)

            btn_login = page.locator('button[type="submit"], input[type="submit"], button:has-text("Entrar"), .btn-primary').first
            if btn_login.is_visible():
                btn_login.click()
            else:
                page.locator(seletor_pass).first.press("Enter")

            page.wait_for_timeout(5000)
            page.wait_for_load_state("networkidle")
        except Exception as err:
            print(f"Aviso no login: {err}")

        # Coleta Matriz
        alunos_matriz = extrair_alunos_unidade(page, matriz_url, "Matriz")

        # Coleta Filial
        alunos_filial = []
        if filial_url and filial_url != matriz_url:
            alunos_filial = extrair_alunos_unidade(page, filial_url, "Filial")

        todos_alunos = alunos_matriz + alunos_filial
        browser.close()
        return todos_alunos

def atualizar_supabase():
    alunos = extrair_alunos_completos()
    total_registros = len(alunos)

    if total_registros == 0:
        print("Aviso: Nenhum aluno de Informática Ativo foi localizado.")
        return

    # Salva arquivo local para commit no repositório
    with open("dados_alunos.json", "w", encoding="utf-8") as f:
        json.dump(alunos, f, ensure_ascii=False, indent=2)

    total_matriz = len([a for a in alunos if a.get("unidade") == "Matriz"])
    total_filial = len([a for a in alunos if a.get("unidade") == "Filial"])
    alunos_criticos = len([a for a in alunos if a.get("criticidade") == "CRITICO"])
    alunos_moderados = len([a for a in alunos if a.get("criticidade") == "MODERADO"])

    payload = {
        "id": 1,
        "total_filial": total_filial,
        "total_matriz": total_matriz,
        "dados_completos": alunos,
        "alunos_criticos": alunos_criticos,
        "alunos_moderados": alunos_moderados,
        "atualizado_em": time.strftime('%Y-%m-%dT%H:%M:%S+00:00')
    }

    supabase.table("resumo_cgd").upsert(payload).execute()
    print(f"Sucesso! {total_registros} alunos gravados no Supabase (Matriz: {total_matriz}, Filial: {total_filial}).")

if __name__ == "__main__":
    atualizar_supabase()
