import os
import json
import time
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
URL_MATRIZ = os.getenv("CGD_MATRIZ_URL")

LOGIN_FILIAL = os.getenv("CGD_USER_FILIAL")
SENHA_FILIAL = os.getenv("CGD_PASS_FILIAL")
URL_FILIAL = os.getenv("CGD_FILIAL_URL")

CGD_URL = os.getenv("CGD_LOGIN_URL") or "https://cgdgestao.com.br"

DIAGNOSTICO_DIR = Path("diagnostico_scraping")
DIAGNOSTICO_DIR.mkdir(parents=True, exist_ok=True)

JSON_PATH = Path("dados_alunos.json")


# ==============================================================================
# FUNÇÕES DE DIAGNÓSTICO
# ==============================================================================

def salvar_diagnostico(page, unidade_nome, etapa):
    """
    Salva screenshot, HTML e informações básicas da página.
    Não salva credenciais.
    """

    try:
        prefixo = unidade_nome.lower().replace(" ", "_")
        etapa_limpa = etapa.lower().replace(" ", "_").replace("/", "_")

        screenshot_path = (
            DIAGNOSTICO_DIR /
            f"{prefixo}_{etapa_limpa}.png"
        )

        html_path = (
            DIAGNOSTICO_DIR /
            f"{prefixo}_{etapa_limpa}.html"
        )

        info_path = (
            DIAGNOSTICO_DIR /
            f"{prefixo}_{etapa_limpa}.txt"
        )

        # Screenshot
        page.screenshot(
            path=str(screenshot_path),
            full_page=True
        )

        # HTML
        html = page.content()

        with open(
            html_path,
            "w",
            encoding="utf-8"
        ) as arquivo:
            arquivo.write(html)

        # Informações básicas
        with open(
            info_path,
            "w",
            encoding="utf-8"
        ) as arquivo:

            arquivo.write(
                f"UNIDADE: {unidade_nome}\n"
            )

            arquivo.write(
                f"ETAPA: {etapa}\n"
            )

            arquivo.write(
                f"URL ATUAL: {page.url}\n"
            )

            arquivo.write(
                f"TÍTULO: {page.title()}\n"
            )

            arquivo.write(
                f"IFRAMES: {len(page.frames)}\n"
            )

            arquivo.write(
                f"INPUTS: {page.locator('input').count()}\n"
            )

            arquivo.write(
                f"BUTTONS: {page.locator('button').count()}\n"
            )

            arquivo.write(
                f"LINKS: {page.locator('a').count()}\n"
            )

            arquivo.write("\n")

            arquivo.write("TEXTOS DOS INPUTS:\n")

            inputs = page.locator("input").all()

            for i, input_element in enumerate(inputs):
                try:
                    tipo = input_element.get_attribute("type")
                    nome = input_element.get_attribute("name")
                    identificador = input_element.get_attribute("id")
                    placeholder = input_element.get_attribute(
                        "placeholder"
                    )

                    arquivo.write(
                        f"[{i}] "
                        f"type={tipo} "
                        f"name={nome} "
                        f"id={identificador} "
                        f"placeholder={placeholder}\n"
                    )

                except Exception:
                    pass

            arquivo.write("\n")
            arquivo.write("FRAMES:\n")

            for i, frame in enumerate(page.frames):
                try:
                    arquivo.write(
                        f"[{i}] URL={frame.url}\n"
                    )
                except Exception:
                    pass

        print(
            f"[DIAGNÓSTICO] Arquivos salvos: "
            f"{screenshot_path}, "
            f"{html_path}, "
            f"{info_path}"
        )

    except Exception as erro:
        print(
            f"[DIAGNÓSTICO] Erro ao salvar diagnóstico "
            f"de {unidade_nome}/{etapa}: {erro}"
        )


def imprimir_estado_pagina(page, unidade_nome, etapa):
    """
    Imprime no log do GitHub Actions informações úteis,
    sem revelar senhas.
    """

    print("")
    print("=" * 80)
    print(f"DIAGNÓSTICO DA PÁGINA - {unidade_nome}")
    print(f"ETAPA: {etapa}")
    print("=" * 80)

    try:
        print(f"URL atual: {page.url}")
    except Exception:
        print("URL atual: não disponível")

    try:
        print(f"Título: {page.title()}")
    except Exception:
        print("Título: não disponível")

    try:
        print(f"Quantidade de inputs: {page.locator('input').count()}")
    except Exception:
        pass

    try:
        print(f"Quantidade de botões: {page.locator('button').count()}")
    except Exception:
        pass

    try:
        print(f"Quantidade de links: {page.locator('a').count()}")
    except Exception:
        pass

    try:
        print(f"Quantidade de frames: {len(page.frames)}")
    except Exception:
        pass

    print("")
    print("Texto visível inicial da página:")

    try:
        texto = page.locator("body").inner_text(
            timeout=10000
        )

        texto = texto[:5000]

        print(texto)

    except Exception as erro:
        print(
            f"Não foi possível obter texto da página: {erro}"
        )

    print("=" * 80)
    print("")


def verificar_elementos_login(page):
    """
    Verifica vários padrões possíveis de login.
    """

    seletores_login = [
        'input[type="text"]',
        'input[type="email"]',
        'input[name*="user" i]',
        'input[name*="login" i]',
        'input[name*="email" i]',
        'input[id*="user" i]',
        'input[id*="login" i]',
        'input[id*="email" i]',
        'input[placeholder*="usuário" i]',
        'input[placeholder*="usuario" i]',
        'input[placeholder*="login" i]',
        'input[placeholder*="email" i]',
    ]

    seletores_senha = [
        'input[type="password"]',
        'input[name*="senha" i]',
        'input[name*="password" i]',
        'input[id*="senha" i]',
        'input[id*="password" i]',
    ]

    print("Verificando campos de login...")

    login_encontrado = None

    for seletor in seletores_login:

        try:
            locator = page.locator(seletor)

            quantidade = locator.count()

            print(
                f"LOGIN selector: {seletor} "
                f"=> {quantidade}"
            )

            if quantidade > 0:

                for i in range(quantidade):

                    try:
                        if locator.nth(i).is_visible():
                            login_encontrado = locator.nth(i)
                            print(
                                f"LOGIN VISÍVEL encontrado com: "
                                f"{seletor}"
                            )
                            break

                    except Exception:
                        pass

            if login_encontrado:
                break

        except Exception:
            pass

    senha_encontrada = None

    print("")
    print("Verificando campo de senha...")

    for seletor in seletores_senha:

        try:
            locator = page.locator(seletor)

            quantidade = locator.count()

            print(
                f"SENHA selector: {seletor} "
                f"=> {quantidade}"
            )

            if quantidade > 0:

                for i in range(quantidade):

                    try:
                        if locator.nth(i).is_visible():
                            senha_encontrada = locator.nth(i)
                            print(
                                f"SENHA VISÍVEL encontrada com: "
                                f"{seletor}"
                            )
                            break

                    except Exception:
                        pass

            if senha_encontrada:
                break

        except Exception:
            pass

    return login_encontrado, senha_encontrada


def localizar_botao_login(page):
    """
    Procura o botão de login utilizando vários padrões.
    """

    seletores = [
        'button[type="submit"]',
        'input[type="submit"]',
        'button:has-text("Entrar")',
        'button:has-text("Acessar")',
        'button:has-text("Login")',
        'button:has-text("Logar")',
        'button:has-text("Continuar")',
        'input[value*="Entrar" i]',
        'input[value*="Acessar" i]',
        'input[value*="Login" i]',
    ]

    for seletor in seletores:

        try:
            locator = page.locator(seletor)

            quantidade = locator.count()

            print(
                f"BOTÃO selector: {seletor} "
                f"=> {quantidade}"
            )

            if quantidade > 0:

                for i in range(quantidade):

                    try:

                        if locator.nth(i).is_visible():

                            print(
                                f"BOTÃO VISÍVEL encontrado com: "
                                f"{seletor}"
                            )

                            return locator.nth(i)

                    except Exception:
                        pass

        except Exception:
            pass

    return None


# ==============================================================================
# CRITICIDADE
# ==============================================================================

def calcular_criticidade_e_dias(data_inicio_str):

    """
    Calcula dias desde o início da disciplina.

    ATENÇÃO:
    Durante o diagnóstico estamos usando a data atual real
    do servidor GitHub Actions.
    """

    if not data_inicio_str:
        return "NORMAL", 0

    try:

        data_inicio_str = data_inicio_str.strip()

        data_inicio = datetime.strptime(
            data_inicio_str,
            "%d/%m/%Y"
        )

        hoje = datetime.now()

        dias = (hoje - data_inicio).days

        if dias >= 90:
    return "critico", dias

elif dias >= 60:
    return "moderado", dias

elif dias >= 30:
    return "atencao", dias

else:
    return "normal", max(0, dias)

    except Exception:

        return "NORMAL", 0


# ==============================================================================
# PROCESSAMENTO DE UMA UNIDADE
# ==============================================================================

def fazer_login_e_extrair(
    page,
    usuario,
    senha,
    url_destino,
    unidade_nome
):

    print("")
    print("#" * 80)
    print(f"INICIANDO PROCESSAMENTO: {unidade_nome}")
    print("#" * 80)

    alunos_capturados = []

    try:

        # ----------------------------------------------------------------------
        # 1. ABRIR CGD
        # ----------------------------------------------------------------------

        print("")
        print(
            f"[{unidade_nome}] Acessando:"
        )

        print(CGD_URL)

        page.goto(
            CGD_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(5000)

        imprimir_estado_pagina(
            page,
            unidade_nome,
            "apos_acessar_cgd"
        )

        salvar_diagnostico(
            page,
            unidade_nome,
            "apos_acessar_cgd"
        )

        # ----------------------------------------------------------------------
        # 2. PROCURAR LOGIN
        # ----------------------------------------------------------------------

        login_input, senha_input = verificar_elementos_login(
            page
        )

        # ----------------------------------------------------------------------
        # 3. TENTAR LOGIN
        # ----------------------------------------------------------------------

        if login_input and senha_input:

            print("")
            print(
                f"[{unidade_nome}] "
                f"Campos de login encontrados."
            )

            print(
                f"[{unidade_nome}] "
                f"Preenchendo credenciais..."
            )

            login_input.fill(usuario)

            senha_input.fill(senha)

            botao = localizar_botao_login(page)

            if botao:

                print(
                    f"[{unidade_nome}] "
                    f"Clicando no botão de login..."
                )

                botao.click()

            else:

                print(
                    f"[{unidade_nome}] "
                    f"ERRO: botão de login não encontrado."
                )

                salvar_diagnostico(
                    page,
                    unidade_nome,
                    "botao_login_nao_encontrado"
                )

                return alunos_capturados

            # Espera após login
            try:

                page.wait_for_load_state(
                    "networkidle",
                    timeout=60000
                )

            except Exception:

                print(
                    f"[{unidade_nome}] "
                    f"Aviso: networkidle não atingido."
                )

            page.wait_for_timeout(5000)

            imprimir_estado_pagina(
                page,
                unidade_nome,
                "apos_login"
            )

            salvar_diagnostico(
                page,
                unidade_nome,
                "apos_login"
            )

        else:

            print("")
            print(
                f"[{unidade_nome}] "
                f"ERRO DIAGNÓSTICO: "
                f"não foram encontrados campos de login visíveis."
            )

            print(
                f"[{unidade_nome}] "
                f"Não vamos simplesmente assumir que já está autenticado."
            )

            salvar_diagnostico(
                page,
                unidade_nome,
                "login_nao_encontrado"
            )

            # Continua apenas para descobrir se a página destino
            # está acessível mesmo sem login.

        # ----------------------------------------------------------------------
        # 4. IR PARA URL DOS ALUNOS
        # ----------------------------------------------------------------------

        if not url_destino:

            print(
                f"[{unidade_nome}] "
                f"ERRO: URL da listagem não configurada."
            )

            return alunos_capturados

        print("")
        print(
            f"[{unidade_nome}] "
            f"Acessando URL dos alunos:"
        )

        print(url_destino)

        page.goto(
            url_destino,
            wait_until="domcontentloaded",
            timeout=60000
        )

        page.wait_for_timeout(5000)

        imprimir_estado_pagina(
            page,
            unidade_nome,
            "pagina_alunos"
        )

        salvar_diagnostico(
            page,
            unidade_nome,
            "pagina_alunos"
        )

        # ----------------------------------------------------------------------
        # 5. VERIFICAR SE A PÁGINA REALMENTE TEM TABELA
        # ----------------------------------------------------------------------

        quantidade_tabelas = page.locator("table").count()

        quantidade_linhas = page.locator(
            "table tbody tr"
        ).count()

        print("")
        print(
            f"[{unidade_nome}] "
            f"Tabelas encontradas: {quantidade_tabelas}"
        )

        print(
            f"[{unidade_nome}] "
            f"Linhas encontradas: {quantidade_linhas}"
        )

        if quantidade_tabelas == 0:

            print("")
            print(
                f"[{unidade_nome}] "
                f"ERRO: nenhuma tabela encontrada."
            )

            print(
                f"[{unidade_nome}] "
                f"Isso indica que a URL provavelmente "
                f"não abriu a listagem esperada."
            )

            salvar_diagnostico(
                page,
                unidade_nome,
                "sem_tabela"
            )

            return alunos_capturados

        # ----------------------------------------------------------------------
        # 6. TENTAR AUMENTAR REGISTROS POR PÁGINA
        # ----------------------------------------------------------------------

        try:

            select_limit = page.locator(
                'select[name*="length" i], '
                'select[name*="limit" i], '
                'select[name*="per_page" i]'
            ).first

            if select_limit.is_visible(timeout=3000):

                print(
                    f"[{unidade_nome}] "
                    f"Controle de quantidade por página encontrado."
                )

                try:

                    select_limit.select_option(
                        value="-1"
                    )

                    page.wait_for_timeout(3000)

                except Exception as erro:

                    print(
                        f"[{unidade_nome}] "
                        f"Não foi possível selecionar -1: {erro}"
                    )

        except Exception:
            pass

        # ----------------------------------------------------------------------
        # 7. PAGINAÇÃO
        # ----------------------------------------------------------------------

        pagina_atual = 1

        while True:

            print("")
            print(
                f"[{unidade_nome}] "
                f"Raspando página {pagina_atual}..."
            )

            page.wait_for_timeout(2000)

            rows = page.locator(
                "table tbody tr"
            ).all()

            print(
                f"[{unidade_nome}] "
                f"Linhas encontradas nesta página: {len(rows)}"
            )

            for indice, row in enumerate(rows):

                try:

                    cols = row.locator(
                        "td"
                    ).all_text_contents()

                    cols = [
                        coluna.strip()
                        for coluna in cols
                    ]

                    print(
                        f"[{unidade_nome}] "
                        f"Linha {indice + 1}: {cols}"
                    )

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
                    # FILTRO STATUS
                    # ----------------------------------------------------------

                    if (
                        "DESATIVADO" in status_texto
                        or "ENCERRADO" in status_texto
                        or "INATIVO" in status_texto
                    ):

                        print(
                            f"[{unidade_nome}] "
                            f"Aluno descartado por status: "
                            f"{nome}"
                        )

                        continue

                    # ----------------------------------------------------------
                    # FILTRO INFORMÁTICA
                    # ----------------------------------------------------------

                    if (
                        "informática" not in curso_texto
                        and "informatica" not in curso_texto
                    ):

                        print(
                            f"[{unidade_nome}] "
                            f"Aluno descartado por curso: "
                            f"{nome} | {curso}"
                        )

                        continue

                    # ----------------------------------------------------------
                    # DATA
                    # ----------------------------------------------------------

                    data_inicio_str = ""

                    if len(cols) > 4:

                        data_inicio_str = cols[4]

                    criticidade, dias_ativos = (
                        calcular_criticidade_e_dias(
                            data_inicio_str
                        )
                    )

                    unidade_db = (
    "matriz"
    if unidade_nome.lower() == "matriz"
    else "filial"
)

criticidade_db = criticidade.lower()

if criticidade_db == "critico":
    tratativa_sugerida = "aulao"
elif criticidade_db == "moderado":
    tratativa_sugerida = "atividade_pratica"
elif criticidade_db == "atencao":
    tratativa_sugerida = "acompanhamento"
else:
    tratativa_sugerida = "normal"

aluno_data = {
    "cgd_matricula_id": contrato,
    "nome": nome,
    "contrato": contrato,

    "email": None,
    "telefone": None,

    "curso": curso,
    "turma_nome": "",
    "professor_nome": "",

    "data_inicio": (
        datetime.strptime(
            data_inicio_str,
            "%d/%m/%Y"
        ).strftime("%Y-%m-%d")
        if data_inicio_str
        else datetime.now().strftime("%Y-%m-%d")
    ),

    "meses_contrato_total": 12,

    "ultima_aula": None,
    "ultimo_acesso": None,

    "faltas_totais": 0,
    "faltas_mes_atual": 0,

    "mes_referencia_faltas": datetime.now().strftime("%m/%Y"),

    "dias_em_curso": dias_ativos,

    "criticidade": criticidade_db,

    "tratativa_sugerida": tratativa_sugerida,

    "status_tratativa": "pendente",

    "status_matricula": "ativo",

    "bloqueado_automaticamente": False,

    "motivo_bloqueio": None,

    "total_disciplinas_grade": 0,

    "disciplinas_concluidas": 0,

    "unidade": unidade_db
}

                    alunos_capturados.append(
                        aluno_data
                    )

                    print(
                        f"[{unidade_nome}] "
                        f"ALUNO CAPTURADO: "
                        f"{nome}"
                    )

                except Exception as erro_linha:

                    print(
                        f"[{unidade_nome}] "
                        f"Erro ao processar linha "
                        f"{indice + 1}: {erro_linha}"
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

                if not btn_proximo.is_visible(timeout=3000):

                    print(
                        f"[{unidade_nome}] "
                        f"Botão de próxima página não encontrado."
                    )

                    break

                # Verifica classes
                classes = (
                    btn_proximo.get_attribute("class")
                    or ""
                ).lower()

                aria_disabled = (
                    btn_proximo.get_attribute(
                        "aria-disabled"
                    )
                    or ""
                ).lower()

                disabled = (
                    btn_proximo.is_disabled()
                    or "disabled" in classes
                    or aria_disabled == "true"
                )

                if disabled:

                    print(
                        f"[{unidade_nome}] "
                        f"Última página alcançada."
                    )

                    break

                print(
                    f"[{unidade_nome}] "
                    f"Avançando para próxima página..."
                )

                btn_proximo.click()

                page.wait_for_timeout(3000)

                pagina_atual += 1

                if pagina_atual > 100:

                    print(
                        f"[{unidade_nome}] "
                        f"Proteção ativada: mais de 100 páginas."
                    )

                    break

            except Exception as erro_paginacao:

                print(
                    f"[{unidade_nome}] "
                    f"Fim da paginação ou erro: "
                    f"{erro_paginacao}"
                )

                break

        # ----------------------------------------------------------------------
        # FINAL DA UNIDADE
        # ----------------------------------------------------------------------

        print("")
        print(
            f"[{unidade_nome}] "
            f"TOTAL DE ALUNOS VÁLIDOS: "
            f"{len(alunos_capturados)}"
        )

        salvar_diagnostico(
            page,
            unidade_nome,
            "final"
        )

    except Exception as erro:

        print("")
        print(
            f"[{unidade_nome}] "
            f"ERRO GERAL: {erro}"
        )

        salvar_diagnostico(
            page,
            unidade_nome,
            "erro_geral"
        )

    return alunos_capturados


# ==============================================================================
# MAIN
# ==============================================================================

def main():

    print("")
    print("=" * 80)
    print("INÍCIO DO SCRAPER CGD")
    print("=" * 80)

    print("")
    print("Verificação das variáveis de ambiente:")

    print(
        f"SUPABASE_URL: "
        f"{'OK' if SUPABASE_URL else 'AUSENTE'}"
    )

    print(
        f"SUPABASE_SERVICE_ROLE_KEY: "
        f"{'OK' if SUPABASE_KEY else 'AUSENTE'}"
    )

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

    if not SUPABASE_URL or not SUPABASE_KEY:

        print("")
        print(
            "ERRO CRÍTICO: "
            "credenciais do Supabase ausentes."
        )

        return

    todos_alunos = []

    # ==========================================================================
    # PLAYWRIGHT
    # ==========================================================================

    with sync_playwright() as p:

        print("")
        print("Iniciando Chromium...")

        browser = p.chromium.launch(
            headless=True
        )

        # Contexto separado para cada unidade
        # evita que uma sessão interfira na outra.

        # ----------------------------------------------------------------------
        # MATRIZ
        # ----------------------------------------------------------------------

        if LOGIN_MATRIZ and SENHA_MATRIZ:

            context_matriz = browser.new_context(
                viewport={
                    "width": 1280,
                    "height": 800
                }
            )

            page_matriz = context_matriz.new_page()

            alunos_matriz = fazer_login_e_extrair(
                page_matriz,
                LOGIN_MATRIZ,
                SENHA_MATRIZ,
                URL_MATRIZ,
                "Matriz"
            )

            todos_alunos.extend(
                alunos_matriz
            )

            context_matriz.close()

        else:

            print(
                "Credenciais da Matriz ausentes."
            )

        # ----------------------------------------------------------------------
        # FILIAL
        # ----------------------------------------------------------------------

        if LOGIN_FILIAL and SENHA_FILIAL:

            context_filial = browser.new_context(
                viewport={
                    "width": 1280,
                    "height": 800
                }
            )

            page_filial = context_filial.new_page()

            alunos_filial = fazer_login_e_extrair(
                page_filial,
                LOGIN_FILIAL,
                SENHA_FILIAL,
                URL_FILIAL,
                "Filial"
            )

            todos_alunos.extend(
                alunos_filial
            )

            context_filial.close()

        else:

            print(
                "Credenciais da Filial ausentes."
            )

        browser.close()

    # ==========================================================================
    # RESULTADO
    # ==========================================================================

    print("")
    print("=" * 80)
    print(
        f"TOTAL GERAL DE ALUNOS CAPTURADOS: "
        f"{len(todos_alunos)}"
    )
    print("=" * 80)

    # ==========================================================================
    # SALVAR JSON LOCAL
    # ==========================================================================

    if todos_alunos:

        with open(
            JSON_PATH,
            "w",
            encoding="utf-8"
        ) as arquivo:

            json.dump(
                todos_alunos,
                arquivo,
                ensure_ascii=False,
                indent=2
            )

        print("")
        print(
            f"dados_alunos.json atualizado com "
            f"{len(todos_alunos)} alunos."
        )

        # ======================================================================
        # SUPABASE
        # ======================================================================

        try:

            print("")
            print(
                "Enviando dados para o Supabase..."
            )

            supabase: Client = create_client(
                SUPABASE_URL,
                SUPABASE_KEY
            )

            data = {
                "relatorio": json.dumps(
                    todos_alunos,
                    ensure_ascii=False
                ),
                "atualizado_em": datetime.now().isoformat()
            }

            res = (
                supabase
                .table("resumo_cgd")
                .upsert(data)
                .execute()
            )

            print(
                "Relatório enviado com sucesso "
                "para resumo_cgd."
            )

            print(
                f"Resposta Supabase: {res}"
            )

        except Exception as erro_supabase:

            print("")
            print(
                "ERRO AO ATUALIZAR SUPABASE:"
            )

            print(erro_supabase)

    else:

        print("")
        print(
            "NENHUM ALUNO VÁLIDO FOI CAPTURADO."
        )

        # Criamos um JSON vazio somente para deixar
        # explícito o resultado do processamento.

        with open(
            JSON_PATH,
            "w",
            encoding="utf-8"
        ) as arquivo:

            json.dump(
                [],
                arquivo,
                ensure_ascii=False,
                indent=2
            )


# ==============================================================================
# EXECUÇÃO
# ==============================================================================

if __name__ == "__main__":
    main()
