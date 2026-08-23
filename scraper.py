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

def extrair_alunos_da_tabela(page, nome_unidade):
    try:
        page.wait_for_selector("table", timeout=20000)
    except Exception:
        print(f"Tabela de alunos não localizada em {nome_unidade}. URL atual: {page.url}")
        return []

    # Tenta expandir a exibição da tabela para mais registros por página
    try:
        select_elem = page.query_selector('select[name*="length"], select[name*="table_length"]')
        if select_elem:
            page.select_option('select[name*="length"], select[name*="table_length"]', '500')
            page.wait_for_timeout(2500)
    except Exception:
        pass

    alunos_coletados = []
    chaves_processadas = set()
    pagina_atual = 1
    MAX_PAGINAS = 50

    while pagina_atual <= MAX_PAGINAS:
        rows = page.query_selector_all("table tbody tr")
        novos_nesta_pagina = 0

        for row in rows:
            cols = row.query_selector_all("td")
            if len(cols) < 4:
                continue

            linha_texto = row.inner_text().lower()

            # Regras de negócio: Somente curso de Informática e Alunos Ativos
            if "informática" not in linha_texto and "informatica" not in linha_texto:
                continue
            if "inativo" in linha_texto or "cancelado" in linha_texto or "trancado" in linha_texto:
                continue

            col_0 = cols[0].inner_text().strip()
            col_1 = cols[1].inner_text().strip()
            col_2 = cols[2].inner_text().strip()

            chave_unica = f"{col_0}_{col_1}"
            if chave_unica in chaves_processadas:
                continue

            chaves_processadas.add(chave_unica)
            novos_nesta_pagina += 1

            col_dias = cols[3].inner_text().strip() if len(cols) >= 4 else "0"
            col_faltas = cols[4].inner_text().strip() if len(cols) >= 5 else "0"

            dias_sem_freq = int(col_dias) if col_dias.isdigit() else 0
            faltas = int(col_faltas) if col_faltas.isdigit() else 0

            criticidade, status_bloqueio = classificar_aluno(dias_sem_freq, faltas)

            aluno = {
                "contrato": col_0,
                "nome": col_1,
                "curso": col_2,
                "unidade": nome_unidade,
                "dias": dias_sem_freq,
                "faltas": faltas,
                "criticidade": criticidade,
                "status": status_bloqueio
            }
            alunos_coletados.append(aluno)

        print(f"Página {pagina_atual} ({nome_unidade}): {novos_nesta_pagina} alunos de informática capturados.")

        if novos_nesta_pagina == 0 and pagina_atual > 1:
            break

        # Navegação para a próxima página da tabela
        next_btn = page.query_selector('.paginate_button.next:not(.disabled), a[rel="next"]:not(.disabled)')
        if next_btn and next_btn.is_visible():
            try:
                next_btn.click()
                pagina_atual += 1
                page.wait_for_timeout(1500)
            except Exception:
                break
        else:
            break

    print(f"Total de alunos extraídos em {nome_unidade}: {len(alunos_coletados)}")
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
        # Define como padrão a URL direta da lista de Alunos
        alunos_url = (os.environ.get("CGD_MATRIZ_URL") or os.environ.get("URL_ALUNOS_MATRIZ") or "https://app.cgd.com.br/alunos").strip()

        usuario = (os.environ.get("CGD_USER_MATRIZ") or os.environ.get("CGD_USER") or "").strip()
        senha = (os.environ.get("CGD_PASS_MATRIZ") or os.environ.get("CGD_PASS") or "").strip()

        print(f"1. Efetuando login em: {login_url}")
        page.goto(login_url, wait_until="domcontentloaded", timeout=25000)

        seletor_user = 'input[name="usuario"], input[name="login"], input[name="email"], input[type="text"]'
        seletor_pass = 'input[type="password"]'

        try:
            page.wait_for_selector(seletor_user, timeout=10000, state="visible")
            page.locator(seletor_user).first.fill(usuario)
            page.locator(seletor_pass).first.fill(senha)
            page.wait_for_timeout(500)

            btn_login = page.locator('button[type="submit"], input[type="submit"], button:has-text("Entrar"), .btn-primary').first
            if btn_login.is_visible():
                btn_login.click()
            else:
                page.locator(seletor_pass).first.press("Enter")

            page.wait_for_timeout(4000)
            page.wait_for_load_state("networkidle")
            print(f"Login efetuado com sucesso. URL logada: {page.url}")
        except Exception as err:
            print(f"Aviso no fluxo de login: {err}")

        # Acessa a página acessível de Alunos
        print(f"Navegando para a lista geral de alunos: {alunos_url}")
        page.goto(alunos_url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(3000)

        alunos = extrair_alunos_da_tabela(page, "Matriz")
        browser.close()
        return alunos

def atualizar_supabase():
    alunos = extrair_alunos_completos()
    total_registros = len(alunos)

    if total_registros == 0:
        print("Aviso: Nenhum aluno capturado pelo scraper.")
        return

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
    print(f"Sucesso! {total_registros} alunos gravados no Supabase.")

if __name__ == "__main__":
    atualizar_supabase()
