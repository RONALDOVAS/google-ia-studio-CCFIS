import os
import json
from datetime import datetime
from pathlib import Path

from playwright.sync_api import sync_playwright
from supabase import create_client, Client


# ==============================================================================
# CONFIGURAÇÕES
# ==============================================================================

SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

LOGIN_MATRIZ = os.getenv("CGD_USER_MATRIZ")
SENHA_MATRIZ = os.getenv("CGD_PASS_MATRIZ")
URL_MATRIZ = os.getenv("URL_ALUNOS_MATRIZ") or os.getenv("CGD_MATRIZ_URL")

LOGIN_FILIAL = os.getenv("CGD_USER_FILIAL")
SENHA_FILIAL = os.getenv("CGD_PASS_FILIAL")
URL_FILIAL = os.getenv("URL_ALUNOS_FILIAL") or os.getenv("CGD_FILIAL_URL")

CGD_URL = os.getenv("CGD_LOGIN_URL") or "https://cgdgestao.com.br"

# Diretório dos arquivos de diagnóstico
DIAGNOSTIC_DIR = Path("diagnostico_scraping")
DIAGNOSTIC_DIR.mkdir(parents=True, exist_ok=True)


# ==============================================================================
# UTILITÁRIOS DE DIAGNÓSTICO
# ==============================================================================

def salvar_diagnostico(page, unidade_nome, etapa):
    """
    Salva evidências da página que o Playwright está enxergando.
    """
    unidade = unidade_nome.lower().replace(" ", "_")
    etapa_limpa = (
        etapa.lower()
        .replace(" ", "_")
        .replace("/", "_")
        .replace("-", "_")
    )

    prefixo = DIAGNOSTIC_DIR / f"{unidade}_{etapa_limpa}"

    print("")
    print("=" * 80)
    print(f"DIAGNÓSTICO: {unidade_nome} - {etapa}")
    print("=" * 80)

    try:
        print(f"URL atual: {page.url}")
    except Exception as e:
        print(f"Não foi possível obter URL: {e}")

    try:
        print(f"Título: {page.title()}")
    except Exception as e:
        print(f"Não foi possível obter título: {e}")

    # Screenshot
    try:
        screenshot_path = f"{prefixo}.png"
        page.screenshot(path=screenshot_path, full_page=True)
        print(f"Screenshot salvo: {screenshot_path}")
    except Exception as e:
        print(f"Erro ao salvar screenshot: {e}")

    # HTML
    try:
        html_path = f"{prefixo}.html"
        html = page.content()
        Path(html_path).write_text(html, encoding="utf-8")
        print(f"HTML salvo: {html_path}")
    except Exception as e:
        print(f"Erro ao salvar HTML: {e}")

    # Texto visível
    try:
        text_path = f"{prefixo}.txt"
        texto = page.locator("body").inner_text(timeout=10000)
        Path(text_path).write_text(texto, encoding="utf-8")
        print(f"Texto da página salvo: {text_path}")

        print("")
        print("--- TEXTO VISÍVEL DA PÁGINA ---")
        print(texto[:5000])
        print("--- FIM DO TEXTO ---")
    except Exception as e:
        print(f"Erro ao obter texto da página: {e}")

    # Informações sobre frames
    try:
        frames_path = f"{prefixo}_frames.txt"

        linhas = []
        linhas.append(f"URL principal: {page.url}")
        linhas.append(f"Quantidade de frames: {len(page.frames)}")
        linhas.append("")

        for indice, frame in enumerate(page.frames):
            linhas.append(f"FRAME {indice}")
            linhas.append(f"URL: {frame.url}")

            try:
                inputs = frame.locator("input").count()
                buttons = frame.locator("button").count()
                selects = frame.locator("select").count()

                linhas.append(f"Inputs: {inputs}")
                linhas.append(f"Buttons: {buttons}")
                linhas.append(f"Selects: {selects}")
            except Exception as e:
                linhas.append(f"Erro ao analisar frame: {e}")

            linhas.append("")

        Path(frames_path).write_text("\n".join(linhas), encoding="utf-8")
        print(f"Informações dos frames salvas: {frames_path}")

    except Exception as e:
        print(f"Erro ao analisar frames: {e}")

    print("=" * 80)
    print("")


def procurar_campo_login(page):
    """
    Procura campos de login na página principal e nos frames.
    Retorna (frame, login_locator, senha_locator, botao_locator)
    """

    seletores_login = [
        'input[type="email"]',
        'input[type="text"]',
        'input[name*="user" i]',
        'input[name*="login" i]',
        'input[name*="usuario" i]',
        'input[id*="user" i]',
        'input[id*="login" i]',
        'input[id*="usuario" i]',
    ]

    seletores_senha = [
        'input[type="password"]',
        'input[name*="pass" i]',
        'input[name*="senha" i]',
    ]

    seletores_botao = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Entrar")',
        'button:has-text("Acessar")',
        'button:has-text("Login")',
        'button:has-text("Logar")',
        'button:has-text("Continuar")',
    ]

    for frame in page.frames:

        login_locator = None
        senha_locator = None
        botao_locator = None

        for seletor in seletores_login:
            try:
                locator = frame.locator(seletor).first

                if locator.count() > 0 and locator.is_visible(timeout=1000):
                    login_locator = locator
                    print(f"Campo de login encontrado com seletor: {seletor}")
                    break

            except Exception:
                pass

        if login_locator is None:
            continue

        for seletor in seletores_senha:
            try:
                locator = frame.locator(seletor).first

                if locator.count() > 0 and locator.is_visible(timeout=1000):
                    senha_locator = locator
                    print(f"Campo de senha encontrado com seletor: {seletor}")
                    break

            except Exception:
                pass

        for seletor in seletores_botao:
            try:
                locator = frame.locator(seletor).first

                if locator.count() > 0 and locator.is_visible(timeout=1000):
                    botao_locator = locator
                    print(f"Botão de login encontrado com seletor: {seletor}")
                    break

            except Exception:
                pass

        return frame, login_locator, senha_locator, botao_locator

    return None, None, None, None


def tabela_existe(page):
    """
    Verifica se existe uma tabela com linhas de dados.
    """

    try:
        tabelas = page.locator("table")
        quantidade_tabelas = tabelas.count()

        print(f"Quantidade de tabelas encontradas: {quantidade_tabelas}")

        if quantidade_tabelas == 0:
            return False

        for indice in range(quantidade_tabelas):
            tabela = tabelas.nth(indice)

            try:
                linhas = tabela.locator("tbody tr").count()

                print(
                    f"Tabela {indice}: "
                    f"{linhas} linha(s) no tbody."
                )

                if linhas > 0:
                    return True

            except Exception:
                pass

    except Exception as e:
        print(f"Erro ao verificar tabela: {e}")

    return False


# ==============================================================================
# CRITICIDADE
# ==============================================================================

def calcular_criticidade_e_dias(data_inicio_str):
    """
    Calcula os dias desde o início da disciplina e define a criticidade.
    """

    if not data_inicio_str or data_inicio_str.strip() == "":
        return "NORMAL", 0

    formatos = [
        "%d/%m/%Y",
        "%d-%m-%Y",
        "%Y-%m-%d",
    ]

    data_inicio = None

    for formato in formatos:
        try:
            data_inicio = datetime.strptime(
                data_inicio_str.strip(),
                formato
            )
            break
        except ValueError:
            continue

    if data_inicio is None:
        return "NORMAL", 0

    hoje = datetime.now().replace(
        hour=0,
        minute=0,
        second=0,
        microsecond=0
    )

    dias = (hoje - data_inicio).days

    if dias > 90:
        return "CRÍTICO", dias
    elif dias > 60:
        return "MODERADO", dias
    elif dias > 30:
        return "ATENÇÃO", dias
    else:
        return "NORMAL", max(0, dias)


# ==============================================================================
# LOGIN + EXTRAÇÃO
# ==============================================================================

def fazer_login_e_extrair(
    browser,
    usuario,
    senha,
    url_destino,
    unidade_nome
):
    print("")
    print("#" * 80)
    print(f"# INICIANDO PROCESSAMENTO: {unidade_nome}")
    print("#" * 80)

    alunos_capturados = []

    # Novo contexto para cada unidade
    context = browser.new_context(
        viewport={
            "width": 1280,
            "height": 800
        }
    )

    page = context.new_page()

    try:

        # ----------------------------------------------------------------------
        # 1. ABRIR LOGIN
        # ----------------------------------------------------------------------

        print(f"Acessando URL de login: {CGD_URL}")

        page.goto(
            CGD_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(5000)

        print(f"URL depois de abrir CGD: {page.url}")
        print(f"Título depois de abrir CGD: {page.title()}")

        salvar_diagnostico(
            page,
            unidade_nome,
            "01_pagina_inicial"
        )

        # ----------------------------------------------------------------------
        # 2. PROCURAR LOGIN
        # ----------------------------------------------------------------------

        print("Procurando campos de login...")

        (
            frame_login,
            login_input,
            senha_input,
            submit_btn
        ) = procurar_campo_login(page)

        if login_input is not None:

            print(f"Login localizado para {unidade_nome}.")

            if senha_input is None:
                print("ERRO: Campo de senha não foi localizado.")
                salvar_diagnostico(
                    page,
                    unidade_nome,
                    "02_login_sem_campo_senha"
                )
                return alunos_capturados

            if submit_btn is None:
                print("ERRO: Botão de login não foi localizado.")
                salvar_diagnostico(
                    page,
                    unidade_nome,
                    "02_login_sem_botao"
                )
                return alunos_capturados

            # --------------------------------------------------------------
            # 3. PREENCHER LOGIN
            # --------------------------------------------------------------

            print("Preenchendo usuário...")

            login_input.fill(usuario)

            print("Preenchendo senha...")

            senha_input.fill(senha)

            salvar_diagnostico(
                page,
                unidade_nome,
                "03_antes_do_login"
            )

            print("Enviando formulário de login...")

            submit_btn.click()

            try:
                page.wait_for_load_state(
                    "networkidle",
                    timeout=60000
                )
            except Exception:
                print(
                    "Aviso: networkidle não foi atingido. "
                    "Continuando após aguardar."
                )

            page.wait_for_timeout(5000)

            print(f"URL após login: {page.url}")
            print(f"Título após login: {page.title()}")

            salvar_diagnostico(
                page,
                unidade_nome,
                "04_depois_do_login"
            )

        else:

            print("")
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            print(f"LOGIN NÃO LOCALIZADO PARA {unidade_nome}")
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            print("")
            print("O scraper NÃO vai considerar que está autenticado.")
            print("As evidências da página foram salvas.")

            salvar_diagnostico(
                page,
                unidade_nome,
                "02_LOGIN_NAO_LOCALIZADO"
            )

        # ----------------------------------------------------------------------
        # 4. IR PARA PÁGINA DE ALUNOS
        # ----------------------------------------------------------------------

        if url_destino:

            print("")
            print(f"URL de alunos configurada para {unidade_nome}:")
            print(url_destino)

            if page.url.rstrip("/") != url_destino.rstrip("/"):

                print("Navegando para a página de alunos...")

                page.goto(
                    url_destino,
                    wait_until="domcontentloaded",
                    timeout=60000
                )

                page.wait_for_timeout(5000)

            print(f"URL final antes da raspagem: {page.url}")
            print(f"Título final antes da raspagem: {page.title()}")

            salvar_diagnostico(
                page,
                unidade_nome,
                "05_pagina_alunos"
            )

        else:

            print(
                f"ERRO: URL de alunos não configurada para "
                f"{unidade_nome}."
            )

            salvar_diagnostico(
                page,
                unidade_nome,
                "05_URL_ALUNOS_NAO_CONFIGURADA"
            )

            return alunos_capturados

        # ----------------------------------------------------------------------
        # 5. VERIFICAR SE CHEGAMOS À TABELA
        # ----------------------------------------------------------------------

        print("")
        print("Verificando existência de tabela de alunos...")

        possui_tabela = tabela_existe(page)

        if not possui_tabela:

            print("")
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            print(f"TABELA DE ALUNOS NÃO ENCONTRADA EM {unidade_nome}")
            print("!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!!")
            print("")

            salvar_diagnostico(
                page,
                unidade_nome,
                "06_TABELA_NAO_ENCONTRADA"
            )

            return alunos_capturados

        print("Tabela encontrada.")

        # ----------------------------------------------------------------------
        # 6. TENTAR ALTERAR LIMITE POR PÁGINA
        # ----------------------------------------------------------------------

        try:

            select_limit = page.locator(
                'select[name*="length" i], '
                'select[name*="limit" i], '
                'select[name*="per_page" i]'
            ).first

            if (
                select_limit.count() > 0
                and select_limit.is_visible(timeout=3000)
            ):

                print("Controle de quantidade por página encontrado.")

                try:
                    select_limit.select_option(value="-1")
                    page.wait_for_timeout(3000)
                    print("Tentativa de selecionar todos os registros realizada.")
                except Exception as e:
                    print(
                        f"Não foi possível selecionar '-1': {e}"
                    )

        except Exception as e:
            print(
                f"Controle de paginação por select não encontrado: {e}"
            )

        # ----------------------------------------------------------------------
        # 7. PAGINAÇÃO
        # ----------------------------------------------------------------------

        pagina_atual = 1
        paginas_processadas = set()

        while True:

            print("")
            print(
                f"Raspando página {pagina_atual} "
                f"de {unidade_nome}..."
            )

            page.wait_for_timeout(2000)

            # Evita loop infinito
            url_atual = page.url

            chave_pagina = (
                pagina_atual,
                url_atual
            )

            if chave_pagina in paginas_processadas:
                print("Página repetida detectada. Encerrando paginação.")
                break

            paginas_processadas.add(chave_pagina)

            rows = page.locator(
                "table tbody tr"
            ).all()

            print(f"Linhas encontradas na página: {len(rows)}")

            for row_index, row in enumerate(rows):

                try:

                    cols = row.locator("td").all_text_contents()

                    cols = [
                        col.strip()
                        for col in cols
                    ]

                    if not cols:
                        continue

                    # Ignora linhas que não sejam dados
                    if len(cols) < 3:
                        continue

                    contrato = cols[0]
                    nome = cols[1]
                    curso = cols[2]

                    curso_texto = curso.lower()

                    status_texto = (
                        cols[3].upper()
                        if len(cols) > 3
                        else "ATIVO"
                    )

                    # ----------------------------------------------------------
                    # DESCARTAR DESATIVADOS
                    # ----------------------------------------------------------

                    if (
                        "DESATIVADO" in status_texto
                        or "ENCERRADO" in status_texto
                        or "INATIVO" in status_texto
                    ):
                        continue

                    # ----------------------------------------------------------
                    # FILTRAR INFORMÁTICA
                    # ----------------------------------------------------------

                    if (
                        "informática" not in curso_texto
                        and "informatica" not in curso_texto
                    ):
                        continue

                    # ----------------------------------------------------------
                    # DATA DE INÍCIO
                    # ----------------------------------------------------------

                    data_inicio_str = ""

                    if len(cols) > 4:
                        data_inicio_str = cols[4]

                    criticidade, dias_ativos = (
                        calcular_criticidade_e_dias(
                            data_inicio_str
                        )
                    )

                    aluno_data = {
                        "contrato": contrato,
                        "nome": nome,
                        "unidade": unidade_nome,
                        "curso": curso,
                        "status": "ATIVO",
                        "dias": dias_ativos,
                        "faltas": 0,
                        "criticidade": criticidade
                    }

                    alunos_capturados.append(aluno_data)

                    print(
                        f"Aluno válido encontrado: "
                        f"{nome} | {curso} | "
                        f"{criticidade} | {dias_ativos} dias"
                    )

                except Exception as e:

                    print(
                        f"Erro ao processar linha {row_index}: {e}"
                    )

            # ------------------------------------------------------------------
            # PRÓXIMA PÁGINA
            # ------------------------------------------------------------------

            try:

                btn_proximo = page.locator(
                    'button:has-text("Próximo"), '
                    'button:has-text("Próxima"), '
                    'a:has-text("Próximo"), '
                    'a:has-text("Próxima"), '
                    '.paginate_button.next, '
                    '[aria-label="Next"], '
                    '[aria-label="Próxima"]'
                ).first

                if btn_proximo.count() == 0:
                    print("Botão de próxima página não encontrado.")
                    break

                if not btn_proximo.is_visible(timeout=3000):
                    print("Botão de próxima página não está visível.")
                    break

                disabled = False

                try:
                    disabled = btn_proximo.is_disabled()
                except Exception:
                    pass

                try:
                    classes = btn_proximo.get_attribute("class") or ""

                    if "disabled" in classes.lower():
                        disabled = True

                except Exception:
                    pass

                if disabled:
                    print("Botão de próxima página está desabilitado.")
                    break

                print("Indo para a próxima página...")

                btn_proximo.click()

                page.wait_for_timeout(3000)

                pagina_atual += 1

            except Exception as e:

                print(
                    f"Não foi possível avançar a paginação: {e}"
                )

                break

        # ----------------------------------------------------------------------
        # RESULTADO DA UNIDADE
        # ----------------------------------------------------------------------

        print("")
        print(
            f"Total de alunos válidos filtrados em "
            f"{unidade_nome}: {len(alunos_capturados)}"
        )

        salvar_diagnostico(
            page,
            unidade_nome,
            "07_final_da_raspagem"
        )

    except Exception as e:

        print("")
        print(
            f"ERRO GERAL AO PROCESSAR {unidade_nome}:"
        )
        print(str(e))

        try:
            salvar_diagnostico(
                page,
                unidade_nome,
                "ERRO_GERAL"
            )
        except Exception:
            pass

    finally:

        try:
            context.close()
        except Exception:
            pass

    return alunos_capturados


# ==============================================================================
# MAIN
# ==============================================================================

def main():

    print("")
    print("=" * 80)
    print("SCRAPER CGD - INÍCIO")
    print("=" * 80)
    print(
        f"Data/hora do processamento: "
        f"{datetime.now().strftime('%d/%m/%Y %H:%M:%S')}"
    )
    print("=" * 80)
    print("")

    # --------------------------------------------------------------------------
    # VALIDAR CONFIGURAÇÃO
    # --------------------------------------------------------------------------

    if not SUPABASE_URL or not SUPABASE_KEY:

        print(
            "ERRO CRÍTICO: SUPABASE_URL ou "
            "SUPABASE_SERVICE_ROLE_KEY não foram encontradas."
        )

        return

    print("Variáveis principais encontradas:")

    print(
        f"CGD_LOGIN_URL: "
        f"{'OK' if CGD_URL else 'AUSENTE'}"
    )

    print(
        f"CGD_USER_MATRIZ: "
        f"{'OK' if LOGIN_MATRIZ else 'AUSENTE'}"
    )

    print(
        f"CGD_PASS_MATRIZ: "
        f"{'OK' if SENHA_MATRIZ else 'AUSENTE'}"
    )

    print(
        f"CGD_MATRIZ_URL: "
        f"{'OK' if URL_MATRIZ else 'AUSENTE'}"
    )

    print(
        f"CGD_USER_FILIAL: "
        f"{'OK' if LOGIN_FILIAL else 'AUSENTE'}"
    )

    print(
        f"CGD_PASS_FILIAL: "
        f"{'OK' if SENHA_FILIAL else 'AUSENTE'}"
    )

    print(
        f"CGD_FILIAL_URL: "
        f"{'OK' if URL_FILIAL else 'AUSENTE'}"
    )

    print("")

    todos_alunos = []

    # --------------------------------------------------------------------------
    # PLAYWRIGHT
    # --------------------------------------------------------------------------

    with sync_playwright() as p:

        browser = p.chromium.launch(
            headless=True
        )

        # ----------------------------------------------------------------------
        # MATRIZ
        # ----------------------------------------------------------------------

        if LOGIN_MATRIZ and SENHA_MATRIZ:

            alunos_matriz = fazer_login_e_extrair(
                browser,
                LOGIN_MATRIZ,
                SENHA_MATRIZ,
                URL_MATRIZ,
                "Matriz"
            )

            todos_alunos.extend(alunos_matriz)

        else:

            print(
                "Aviso: credenciais da Matriz não encontradas."
            )

        # ----------------------------------------------------------------------
        # FILIAL
        # ----------------------------------------------------------------------

        if LOGIN_FILIAL and SENHA_FILIAL:

            alunos_filial = fazer_login_e_extrair(
                browser,
                LOGIN_FILIAL,
                SENHA_FILIAL,
                URL_FILIAL,
                "Filial"
            )

            todos_alunos.extend(alunos_filial)

        else:

            print(
                "Aviso: credenciais da Filial não encontradas."
            )

        browser.close()

    # --------------------------------------------------------------------------
    # RESULTADO FINAL
    # --------------------------------------------------------------------------

    print("")
    print("=" * 80)
    print(
        f"FIM DA RASPAGEM. "
        f"TOTAL GERAL: {len(todos_alunos)}"
    )
    print("=" * 80)
    print("")

    # --------------------------------------------------------------------------
    # SALVAR JSON
    # --------------------------------------------------------------------------

    try:

        json_path = Path("dados_alunos.json")

        json_path.write_text(
            json.dumps(
                todos_alunos,
                ensure_ascii=False,
                indent=2
            ),
            encoding="utf-8"
        )

        print(
            f"dados_alunos.json atualizado com "
            f"{len(todos_alunos)} registro(s)."
        )

    except Exception as e:

        print(
            f"Erro ao salvar dados_alunos.json: {e}"
        )

    # --------------------------------------------------------------------------
    # SUPABASE
    # --------------------------------------------------------------------------

    if todos_alunos:

        try:

            print("")
            print("Enviando dados para o Supabase...")

            supabase: Client = create_client(
                SUPABASE_URL,
                SUPABASE_KEY
            )

            data = {
                "relatorio": json.dumps(
                    todos_alunos,
                    ensure_ascii=False
                ),
                "atualizado_em": "now()"
            }

            supabase.table(
                "resumo_cgd"
            ).upsert(data).execute()

            print(
                "Relatório enviado com sucesso "
                "para resumo_cgd."
            )

        except Exception as e:

            print(
                f"ERRO AO ATUALIZAR SUPABASE: {e}"
            )

    else:

        print(
            "Nenhum aluno válido foi capturado. "
            "Supabase NÃO será atualizado."
        )

    print("")
    print(
        f"Arquivos de diagnóstico disponíveis em: "
        f"{DIAGNOSTIC_DIR}"
    )
    print("")


if __name__ == "__main__":
    main()
