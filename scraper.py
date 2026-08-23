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
    faltas_efetivas = max(0, faltas - reposicoes)
    if faltas_efetivas >= 3 or dias_sem_frequencia >= 90:
        return "Crítico", "Aulão de Recuperação"
    elif faltas_efetivas == 2 or (60 <= dias_sem_frequencia < 90):
        return "Moderado", "Atividade Prática Reforço"
    elif faltas_efetivas == 1 or (30 <= dias_sem_frequencia < 60):
        return "Atenção", "Acompanhamento Individual"
    else:
        return "Normal", "Sem Tratativa Necessária"

def extrair_detalhes_contrato(page, id_interno):
    cursos_em_andamento, cursos_a_fazer, ultimo_passo = [], [], "-"
    if not id_interno:
        return {"andamento": "Módulo Ativo", "pendentes": "Consultar no CGD", "ultimo_passo": "-"}

    try:
        page.goto(f"https://app.cgd.com.br/contratos/cursos/{id_interno}", wait_until="domcontentloaded", timeout=10000)
        page.wait_for_timeout(600)
        
        rows_andamento = page.query_selector_all("text='Cursos em andamento' >> xpath=ancestor::div[contains(@class,'card') or contains(@class,'panel')]//table//tbody//tr")
        for r in rows_andamento:
            txt = r.inner_text().split("\t")[0].strip()
            if txt and "nenhum" not in txt.lower():
                cursos_em_andamento.append(txt)

        rows_fazer = page.query_selector_all("text='Cursos a fazer' >> xpath=ancestor::div[contains(@class,'card') or contains(@class,'panel')]//table//tbody//tr")
        for r in rows_fazer:
            txt = r.inner_text().split("\t")[0].strip()
            if txt and "nenhum" not in txt.lower():
                cursos_a_fazer.append(txt)
    except Exception:
        pass

    try:
        page.goto(f"https://app.cgd.com.br/contratos/frequencias/{id_interno}", wait_until="domcontentloaded", timeout=10000)
        page.wait_for_timeout(600)
        first_row_passo = page.query_selector("table tbody tr td:nth-child(7)")
        if first_row_passo:
            ultimo_passo = first_row_passo.inner_text().strip() or "-"
    except Exception:
        pass

    return {
        "andamento": ", ".join(cursos_em_andamento) if cursos_em_andamento else "Módulo Ativo",
        "pendentes": ", ".join(cursos_a_fazer) if cursos_a_fazer else "Nenhuma Pendência",
        "ultimo_passo": ultimo_passo
    }

def extrair_alunos_da_tabela(page, nome_unidade):
    try:
        page.wait_for_selector("table", timeout=15000)
    except Exception:
        print(f"Tabela de alunos não localizada em {nome_unidade}.")
        return []

    try:
        select_elem = page.query_selector('select[name*="length"]')
        if select_elem:
            page.select_option('select[name*="length"]', '500')
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
            if "inativo" in linha_texto or "cancelado" in linha_texto or "trancado" in linha_texto:
                continue

            link_elem = row.query_selector("a[href*='/contratos/']")
            href = link_elem.get_attribute("href") if link_elem else ""
            id_interno = href.split("/contratos/")[1].split("?")[0].strip() if "/contratos/" in href else ""

            num_contrato = cols[0].inner_text().strip()
            nome_aluno = cols[1].inner_text().strip()

            chave_unica = f"{nome_unidade}_{num_contrato}_{nome_aluno}"
            if chave_unica in chaves_processadas:
                continue

            chaves_processadas.add(chave_unica)
            novos_nesta_pagina += 1

            col_dias = cols[3].inner_text().strip() if len(cols) >= 4 else "0"
            col_faltas = cols[4].inner_text().strip() if len(cols) >= 5 else "0"
            col_reposicoes = cols[5].inner_text().strip() if len(cols) >= 6 else "0"

            dias_sem_freq = int(col_dias) if col_dias.isdigit() else 0
            faltas = int(col_faltas) if col_faltas.isdigit() else 0
            reposicoes = int(col_reposicoes) if col_reposicoes.isdigit() else 0
            faltas_efetivas = max(0, faltas - reposicoes)

            criticidade, tratativa = classificar_aluno(dias_sem_freq, faltas, reposicoes)
            link_cgd_aluno = f"https://app.cgd.com.br/contratos/{id_interno}" if id_interno else f"https://app.cgd.com.br/alunos?busca={num_contrato}"

            detalhes = extrair_detalhes_contrato(page, id_interno)

            aluno = {
                "id_interno": id_interno,
                "contrato": num_contrato,
                "nome": nome_aluno,
                "unidade": nome_unidade,
                "dias": dias_sem_freq,
                "faltas": faltas,
                "reposicoes": reposicoes,
                "faltas_efetivas": faltas_efetivas,
                "criticidade": criticidade,
                "status": criticidade.upper(),
                "tratativa": tratativa,
                "status_tratativa": "Pendente" if criticidade != "Normal" else "Normal",
                "ultimo_acesso": f"Há {dias_sem_freq} dia(s)" if dias_sem_freq > 0 else "Hoje",
                "disciplina_andamento": detalhes["andamento"],
                "disciplinas_pendentes": detalhes["pendentes"],
                "passo_atual": detalhes["ultimo_passo"],
                "curso": detalhes["andamento"],
                "link_cgd": link_cgd_aluno
            }
            alunos_coletados.append(aluno)

        print(f"Página {pagina_atual} ({nome_unidade}): {novos_nesta_pagina} alunos processados.")

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

    return alunos_coletados

def processar_unidade(browser, nome_unidade, usuario, senha, alunos_url):
    context = browser.new_context(
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        viewport={"width": 1366, "height": 768}
    )
    page = context.new_page()
    if STEALTH_ATIVO:
        stealth_sync(page)

    print(f"\n--- Processando {nome_unidade} ---")
    page.goto("https://app.cgd.com.br", wait_until="domcontentloaded", timeout=25000)

    try:
        page.fill('input[type="text"], input[name="usuario"], input[name="login"]', usuario)
        page.fill('input[type="password"]', senha)
        page.click('button[type="submit"], input[type="submit"]')
        page.wait_for_timeout(3000)
    except Exception as err:
        print(f"Aviso no login ({nome_unidade}): {err}")

    page.goto(alunos_url, wait_until="domcontentloaded", timeout=25000)
    page.wait_for_timeout(2000)

    alunos = extrair_alunos_da_tabela(page, nome_unidade)
    context.close()
    return alunos

def atualizar_supabase():
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])

        user_matriz = os.environ.get("CGD_USER_MATRIZ") or os.environ.get("CGD_USER") or ""
        pass_matriz = os.environ.get("CGD_PASS_MATRIZ") or os.environ.get("CGD_PASS") or ""
        user_filial = os.environ.get("CGD_USER_FILIAL") or user_matriz
        pass_filial = os.environ.get("CGD_PASS_FILIAL") or pass_matriz

        alunos_matriz = processar_unidade(browser, "Matriz", user_matriz, pass_matriz, "https://app.cgd.com.br/alunos")
        alunos_filial = processar_unidade(browser, "Filial", user_filial, pass_filial, "https://app.cgd.com.br/alunos")

        browser.close()
        todos_alunos = alunos_matriz + alunos_filial

        if not todos_alunos:
            print("Aviso: Nenhum aluno capturado.")
            return

        # GRAVA NOS DOIS FORMATOS PARA GARANTIR COMPATIBILIDADE TOTAL
        relatorio_compatibilidade = json.dumps({
            "total_matriz": len(alunos_matriz),
            "total_filial": len(alunos_filial),
            "detalhes": todos_alunos
        })

        payload = {
            "id": 1,
            "total_filial": len(alunos_filial),
            "total_matriz": len(alunos_matriz),
            "dados_completos": todos_alunos,
            "relatorio": relatorio_compatibilidade,
            "alunos_criticos": len([a for a in todos_alunos if a.get("criticidade") == "Crítico"]),
            "alunos_moderados": len([a for a in todos_alunos if a.get("criticidade") == "Moderado"]),
            "atualizado_em": time.strftime('%Y-%m-%dT%H:%M:%S+00:00')
        }

        supabase.table("resumo_cgd").upsert(payload).execute()
        print(f"\nSucesso! {len(todos_alunos)} alunos salvos no Supabase com compatibilidade dupla.")

if __name__ == "__main__":
    atualizar_supabase()
