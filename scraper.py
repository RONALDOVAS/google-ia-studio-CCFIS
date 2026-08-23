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

def classificar_aluno(dias_sem_frequencia, faltas, reposicoes=0):
    # Cada reposição anula uma falta
    faltas_efetivas = max(0, faltas - reposicoes)

    # Regra de Bloqueio: 3 faltas efetivas ou 90 dias de inatividade
    if faltas_efetivas >= 3 or dias_sem_frequencia >= 90:
        return "Crítico", "BLOQUEADO"
    elif faltas_efetivas == 2 or (60 <= dias_sem_frequencia < 90):
        return "Moderado", "ATENÇÃO"
    elif faltas_efetivas == 1 or (30 <= dias_sem_frequencia < 60):
        return "Atenção", "OBSERVAÇÃO"
    else:
        return "Normal", "REGULAR"

def extrair_alunos_da_tabela(page, nome_unidade):
    try:
        page.wait_for_selector("table", timeout=15000)
    except Exception:
        print(f"Tabela de alunos não localizada em {nome_unidade}. URL: {page.url}")
        return []

    try:
        select_elem = page.query_selector('select[name*="length"], select[name*="table_length"]')
        if select_elem:
            page.select_option('select[name*="length"], select[name*="table_length"]', '500')
            page.wait_for_timeout(2000)
    except Exception:
        pass

    alunos_coletados = []
    chaves_processadas = set()
    pagina_atual = 1
    MAX_PAGINAS = 30

    while pagina_atual <= MAX_PAGINAS:
        rows = page.query_selector_all("table tbody tr")
        novos_nesta_pagina = 0

        for row in rows:
            cols = row.query_selector_all("td")
            if len(cols) < 2:
                continue

            linha_texto = row.inner_text().lower()

            # Descarte de inativos
            if "inativo" in linha_texto or "cancelado" in linha_texto or "trancado" in linha_texto:
                continue

            # Descarte de Formação Profissional
            if "formação profissional" in linha_texto or "formacao profissional" in linha_texto or "profissionalizante" in linha_texto:
                continue

            col_0 = cols[0].inner_text().strip()  # Contrato
            col_1 = cols[1].inner_text().strip()  # Nome
            col_2 = cols[2].inner_text().strip() if len(cols) >= 3 else ""  # Curso Real / Pacote

            chave_unica = f"{nome_unidade}_{col_0}_{col_1}"
            if chave_unica in chaves_processadas:
                continue

            chaves_processadas.add(chave_unica)
            novos_nesta_pagina += 1

            col_dias = cols[3].inner_text().strip() if len(cols) >= 4 else "0"
            col_faltas = cols[4].inner_text().strip() if len(cols) >= 5 else "0"
            col_reposicoes = cols[5].inner_text().strip() if len(cols) >= 6 else "0"
            col_disciplina = cols[6].inner_text().strip() if len(cols) >= 7 else ""
            col_ultimo_acesso = cols[7].inner_text().strip() if len(cols) >= 8 else ""

            dias_sem_freq = int(col_dias) if col_dias.isdigit() else 0
            faltas = int(col_faltas) if col_faltas.isdigit() else 0
            reposicoes = int(col_reposicoes) if col_reposicoes.isdigit() else 0

            # Aplicação da classificação com cálculo de reposição
            criticidade, status_bloqueio = classificar_aluno(dias_sem_freq, faltas, reposicoes)

            # Formatação do texto do último acesso baseado nos dias reais
            if col_ultimo_acesso:
                texto_ultimo_acesso = col_ultimo_acesso
            elif dias_sem_freq == 0:
                texto_ultimo_acesso = "Sem registros de ausência"
            else:
                texto_ultimo_acesso = f"Há {dias_sem_freq} dia(s)"

            aluno = {
                "contrato": col_0,
                "nome": col_1,
                "curso": col_2,
                "unidade": nome_unidade,
                "dias": dias_sem_freq,
                "faltas": faltas,
                "reposicoes": reposicoes,
                "faltas_efetivas": max(0, faltas - reposicoes),
                "criticidade": criticidade,
                "status": status_bloqueio,
                "status_tratativa": "Pendente" if criticidade != "Normal" else "Normal",
                "ultimo_acesso": texto_ultimo_acesso,
                "disciplina_andamento": col_disciplina,
                "link_cgd": f"https://app.cgd.com.br/contratos/{col_0}"
            }
            alunos_coletados.append(aluno)

        print(f"Página {pagina_atual} ({nome_unidade}): {novos_nesta_pagina} alunos válidos capturados.")

        if novos_nesta_pagina == 0 and pagina_atual > 1:
            break

        next_btn = page.query_selector('.paginate_button.next:not(.disabled), a[rel="next"]:not(.disabled)')
        if next_btn and next_btn.is_visible():
            try:
                next_btn.click()
                pagina_atual += 1
                page.wait_for_timeout(1200)
            except Exception:
                break
        else:
            break

    print(f"Total extraído em {nome_unidade}: {len(alunos_coletados)} alunos.")
    return alunos_coletados

def processar_unidade(browser, nome_unidade, usuario, senha, alunos_url):
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1366, "height": 768},
        locale="pt-BR"
    )
    page = context.new_page()
    if STEALTH_ATIVO:
        stealth_sync(page)

    login_url = "https://app.cgd.com.br"
    print(f"\n--- Processando {nome_unidade} ---")
    
    page.goto(login_url, wait_until="domcontentloaded", timeout=25000)

    seletor_user = 'input[name="usuario"], input[name="login"], input[name="email"], input[type="text"]'
    seletor_pass = 'input[type="password"]'

    try:
        page.wait_for_selector(seletor_user, timeout=10000, state="visible")
        page.locator(seletor_user).first.fill(usuario)
        page.locator(seletor_pass).first.fill(senha)
        page.wait_for_timeout(300)

        btn_login = page.locator('button[type="submit"], input[type="submit"], button:has-text("Entrar"), .btn-primary').first
        if btn_login.is_visible():
            btn_login.click()
        else:
            page.locator(seletor_pass).first.press("Enter")

        page.wait_for_timeout(3000)
    except Exception as err:
        print(f"Aviso no login ({nome_unidade}): {err}")

    print(f"Navegando para lista de alunos ({nome_unidade}): {alunos_url}")
    page.goto(alunos_url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(2500)

    alunos = extrair_alunos_da_tabela(page, nome_unidade)
    
    try:
        page.goto("https://app.cgd.com.br/logout", timeout=5000)
    except Exception:
        pass
    context.close()
    
    return alunos

def extrair_todos_alunos():
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

        user_matriz = (os.environ.get("CGD_USER_MATRIZ") or os.environ.get("CGD_USER") or "").strip()
        pass_matriz = (os.environ.get("CGD_PASS_MATRIZ") or os.environ.get("CGD_PASS") or "").strip()
        url_matriz = "https://app.cgd.com.br/alunos"

        user_filial = (os.environ.get("CGD_USER_FILIAL") or user_matriz).strip()
        pass_filial = (os.environ.get("CGD_PASS_FILIAL") or pass_matriz).strip()
        url_filial = "https://app.cgd.com.br/alunos"

        alunos_matriz = processar_unidade(browser, "Matriz", user_matriz, pass_matriz, url_matriz)
        alunos_filial = processar_unidade(browser, "Filial", user_filial, pass_filial, url_filial)

        browser.close()
        return alunos_matriz + alunos_filial

def atualizar_supabase():
    alunos = extrair_todos_alunos()
    total_registros = len(alunos)

    if total_registros == 0:
        print("Aviso: Nenhum aluno capturado pelo scraper.")
        return

    with open("dados_alunos.json", "w", encoding="utf-8") as f:
        json.dump(alunos, f, ensure_ascii=False, indent=2)

    total_matriz = len([a for a in alunos if a.get("unidade") == "Matriz"])
    total_filial = len([a for a in alunos if a.get("unidade") == "Filial"])
    alunos_criticos = len([a for a in alunos if a.get("criticidade") == "Crítico"])
    alunos_moderados = len([a for a in alunos if a.get("criticidade") == "Moderado"])

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
    print(f"\nSucesso! {total_registros} alunos gravados no Supabase (Matriz: {total_matriz} | Filial: {total_filial}).")

if __name__ == "__main__":
    atualizar_supabase()
