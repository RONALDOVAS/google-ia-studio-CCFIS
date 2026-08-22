import os
import json
import re
from playwright.sync_api import sync_playwright
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def extrair_todos_os_alunos_paginados(page, nome_unidade):
    """Percorre e extrai todas as páginas da tabela do CGD."""
    print(f"[{nome_unidade}] Aguardando carregamento do painel de alunos...")
    try:
        page.wait_for_selector('table tbody tr', state="visible", timeout=20000)
    except Exception as e:
        print(f"[{nome_unidade}] Erro: Tabela não carregada - {e}")
        return []

    # Tenta alterar a paginação para o máximo (ex: 100 ou 'Todos')
    try:
        select_elem = page.locator('select[name*="length"], select[name*="table_length"]').first
        if select_elem.is_visible():
            options = select_elem.locator('option').all_inner_texts()
            for opt_target in ["100", "500", "Todos", "All"]:
                matched = [o for o in options if opt_target.lower() in o.lower()]
                if matched:
                    select_elem.select_option(label=matched[0])
                    page.wait_for_timeout(3000)
                    break
    except Exception as e:
        print(f"[{nome_unidade}] Aviso na seleção de limite: {e}")

    todos_alunos = []
    pagina_atual = 1
    max_paginas = 100 # Trava de segurança para loops infinitos

    while pagina_atual <= max_paginas:
        # Extrai linhas da página visível
        alunos_pagina = page.evaluate("""(unidade) => {
            const rows = Array.from(document.querySelectorAll('table tbody tr'));
            return rows.map(row => {
                const cols = Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim());
                if (cols.length < 2) return null;

                const contrato = cols[0] || '';
                const nome = cols[1] || 'Aluno';
                const curso = cols[2] || '';
                const acessoOuDias = cols[3] || '0';

                const matchDias = acessoOuDias.match(/(\\d+)\\s*dias?/i);
                const dias = matchDias ? parseInt(matchDias[1]) : 0;

                let status = 'NORMAL';
                if (dias > 90) status = 'CRÍTICO';
                else if (dias >= 60) status = 'MODERADO';
                else if (dias >= 30) status = 'ATENÇÃO';

                return {
                    contrato: contrato,
                    nome: nome,
                    unidade: unidade,
                    curso: curso,
                    status: status,
                    dias: dias,
                    faltas: 0
                };
            }).filter(a => a !== null);
        }""", nome_unidade)

        if alunos_pagina:
            todos_alunos.extend(alunos_pagina)

        # Procura o botão 'Próximo' ou 'Next' do DataTables
        next_button = page.locator('.paginate_button.next:not(.disabled), li.next:not(.disabled) a, button:has-text("Próximo")').first
        if next_button.is_visible() and next_button.is_enabled():
            print(f"[{nome_unidade}] Carregando página {pagina_atual + 1}...")
            next_button.click()
            page.wait_for_timeout(1500)
            pagina_atual += 1
        else:
            print(f"[{nome_unidade}] Fim da paginação alcançado.")
            break

    # Remove possíveis duplicados pelo contrato/nome
    alunos_unicos = {a['contrato'] + a['nome']: a for a in todos_alunos}.values()
    print(f"[{nome_unidade}] Total consolidado: {len(alunos_unicos)} alunos reais.")
    return list(alunos_unicos)

def efetuar_login_e_extrair(browser, url_login, usuario, senha, nome_unidade):
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    )
    page = context.new_page()

    try:
        print(f"[{nome_unidade}] Efetuando login em {url_login}...")
        page.goto(url_login, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2000)

        page.locator('#login-email, input[name="email"], input[type="email"]').first.fill(usuario)
        page.locator('input[name="password"], input[type="password"]').first.fill(senha)

        submit_btn = page.locator('button[type="submit"], input[type="submit"]').first
        if submit_btn.is_visible():
            submit_btn.click()
        else:
            page.keyboard.press("Enter")

        page.wait_for_load_state("domcontentloaded")
        page.wait_for_timeout(4000)

        # Clica no menu 'Alunos'
        menu_alunos = page.locator('a:has-text("Alunos"), span:has-text("Alunos")').first
        if menu_alunos.is_visible():
            menu_alunos.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(4000)

        alunos = extrair_todos_os_alunos_paginados(page, nome_unidade)
        return alunos

    except Exception as e:
        print(f"[{nome_unidade}] Erro durante extração: {e}")
        return []
    finally:
        context.close()

def efetuar_scraping_cgd():
    login_url = os.environ.get("CGD_LOGIN_URL")
    user_matriz = os.environ.get("CGD_USER_MATRIZ")
    pass_matriz = os.environ.get("CGD_PASS_MATRIZ")
    user_filial = os.environ.get("CGD_USER_FILIAL")
    pass_filial = os.environ.get("CGD_PASS_FILIAL") or os.environ.get("CDG_PASS_FILIAL")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-setuid-sandbox"])

        alunos_matriz = efetuar_login_e_extrair(browser, login_url, user_matriz, pass_matriz, "Matriz")
        alunos_filial = []
        if user_filial and pass_filial:
            alunos_filial = efetuar_login_e_extrair(browser, login_url, user_filial, pass_filial, "Filial")

        browser.close()

    return {
        "total_matriz": len(alunos_matriz),
        "total_filial": len(alunos_filial),
        "detalhes": alunos_matriz + alunos_filial
    }

def salvar_no_supabase(dados):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Supabase não configurado.")
        return

    supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
    
    detalhes = dados.get("detalhes", [])
    criticos = sum(1 for a in detalhes if a.get("status") == "CRÍTICO")
    moderados = sum(1 for a in detalhes if a.get("status") == "MODERADO")

    payload = {
        "id": 1,
        "relatorio": json.dumps(dados, ensure_ascii=False),
        "total_filial": dados.get("total_filial", 0),
        "total_matriz": dados.get("total_matriz", 0),
        "alunos_criticos": criticos,
        "alunos_moderados": moderados,
        "dados_completos": detalhes,
        "atualizado_em": "now()"
    }

    supabase.table("resumo_cgd").upsert(payload).execute()
    print("Sucesso: Supabase atualizado com todos os registros raspados!")

if __name__ == "__main__":
    print("Iniciando extração com varredura de páginas...")
    dados = efetuar_scraping_cgd()
    print(f"Total geral raspado: {len(dados['detalhes'])} alunos.")
    salvar_no_supabase(dados)
    print("Finalizado!")
