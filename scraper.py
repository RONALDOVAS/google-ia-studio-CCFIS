import os
import json
import re
from playwright.sync_api import sync_playwright
from supabase import create_client

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")

def extrair_alunos_da_tabela(page, nome_unidade):
    """Lê diretamente o DOM/HTML da tabela e transforma cada linha em objeto JSON real."""
    print(f"[{nome_unidade}] Aguardando carregamento da tabela de alunos...")
    try:
        page.wait_for_selector('table tbody tr', state="visible", timeout=20000)
    except Exception as e:
        print(f"[{nome_unidade}] Tabela não encontrada ou vazia: {e}")
        return []

    # Tenta selecionar a exibição máxima no Select (ex: 'Todos' ou '500')
    try:
        selects = page.locator('select[name*="length"], select[name*="table"]').all()
        for sel in selects:
            if sel.is_visible():
                options = sel.locator("option").all_inner_texts()
                for target in ["todos", "all", "500", "100"]:
                    matched = [o for o in options if target in o.lower()]
                    if matched:
                        sel.select_option(label=matched[0])
                        page.wait_for_timeout(4000)
                        break
    except Exception as e:
        print(f"[{nome_unidade}] Aviso na paginação: {e}")

    # Extração direta dos nós do HTML via JavaScript do navegador
    alunos = page.evaluate("""(unidade) => {
        const rows = Array.from(document.querySelectorAll('table tbody tr'));
        return rows.map(row => {
            const cols = Array.from(row.querySelectorAll('td')).map(td => td.innerText.trim());
            if (cols.length < 2) return null;

            // Mapeamento baseado nas colunas padrão do CGD
            const contrato = cols[0] || '';
            const nome = cols[1] || 'Aluno sem nome';
            const curso = cols[2] || '';
            const acessoOuDias = cols[3] || '';
            
            // Tenta extrair número de dias se presente, caso contrário define padrão 0
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

    print(f"[{nome_unidade}] Sucesso: {len(alunos)} alunos reais extraídos diretamente!")
    return alunos

def efetuar_login_e_extrair(browser, url_login, usuario, senha, nome_unidade):
    context = browser.new_context(
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    page = context.new_page()

    try:
        print(f"[{nome_unidade}] Efetuando login...")
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

        # Clica no menu Alunos
        menu_alunos = page.locator('a:has-text("Alunos"), span:has-text("Alunos")').first
        if menu_alunos.is_visible():
            menu_alunos.click()
            page.wait_for_load_state("domcontentloaded")
            page.wait_for_timeout(4000)

        alunos = extrair_alunos_da_tabela(page, nome_unidade)
        return alunos

    except Exception as e:
        print(f"[{nome_unidade}] Erro durante execução: {e}")
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

    todos_alunos = alunos_matriz + alunos_filial
    return {
        "total_matriz": len(alunos_matriz),
        "total_filial": len(alunos_filial),
        "detalhes": todos_alunos
    }

def salvar_no_supabase(dados):
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("Aviso: Supabase não configurado.")
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
    print("Sucesso: Supabase atualizado com dados 100% extraídos!")

if __name__ == "__main__":
    print("Iniciando extração total direta do DOM...")
    dados = efetuar_scraping_cgd()
    print(f"Total capturado: {len(dados['detalhes'])} alunos.")
    print("Gravando no Supabase...")
    salvar_no_supabase(dados)
    print("Finalizado!")
