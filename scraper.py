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
    Não salva credenciais diretamente.
    """

    try:
        prefixo = unidade_nome.lower().replace(" ", "_")
        etapa_limpa = (
            etapa.lower()
            .replace(" ", "_")
            .replace("/", "_")
        )

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

        # ----------------------------------------------------------------------
        # SCREENSHOT
        # ----------------------------------------------------------------------

        page.screenshot(
            path=str(screenshot_path),
            full_page=True
        )

        # ----------------------------------------------------------------------
        # HTML
        # ----------------------------------------------------------------------

        html = page.content()

        with open(
            html_path,
            "w",
            encoding="utf-8"
        ) as arquivo:
            arquivo.write(html)

        # ----------------------------------------------------------------------
        # INFORMAÇÕES BÁSICAS
        # ----------------------------------------------------------------------

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

            # ------------------------------------------------------------------
            # INPUTS
            # ------------------------------------------------------------------

            arquivo.write(
                "TEXTOS DOS INPUTS:\n"
            )

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

            # ------------------------------------------------------------------
            # FRAMES
            # ------------------------------------------------------------------

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


# ==============================================================================
# DIAGNÓSTICO DE NAVEGAÇÃO DO CGD - ETAPA 3A
# ==============================================================================

def diagnosticar_navegacao_cgd(page, unidade_nome):
    """
    Diagnóstico específico da navegação disponível no CGD.

    Objetivo da Etapa 3A:
    descobrir quais menus, links, botões e possíveis áreas de
    frequência/faltas estão disponíveis após o login.

    IMPORTANTE:
    esta função NÃO altera dados e NÃO tenta registrar faltas.
    """

    print("")
    print("=" * 80)
    print(
        f"[{unidade_nome}] ETAPA 3A - "
        f"DIAGNÓSTICO DA NAVEGAÇÃO DO CGD"
    )
    print("=" * 80)

    prefixo = unidade_nome.lower().replace(" ", "_")

    diagnostico_path = (
        DIAGNOSTICO_DIR /
        f"{prefixo}_navegacao_cgd.txt"
    )

    palavras_interesse = [
        "falta",
        "faltas",
        "frequencia",
        "frequência",
        "presenca",
        "presença",
        "aula",
        "aulas",
        "aluno",
        "alunos",
        "relatorio",
        "relatório",
        "relatorios",
        "relatórios",
        "operacional",
        "operacionais",
        "chamada",
        "presente",
        "ausente",
        "frequências",
    ]

    try:

        with open(
            diagnostico_path,
            "w",
            encoding="utf-8"
        ) as arquivo:

            arquivo.write(
                "===============================================================================\n"
            )

            arquivo.write(
                "DIAGNÓSTICO DE NAVEGAÇÃO DO CGD - ETAPA 3A\n"
            )

            arquivo.write(
                "===============================================================================\n\n"
            )

            arquivo.write(
                f"UNIDADE: {unidade_nome}\n"
            )

            arquivo.write(
                f"URL ATUAL: {page.url}\n"
            )

            arquivo.write(
                f"TÍTULO: {page.title()}\n\n"
            )

            # ------------------------------------------------------------------
            # URL
            # ------------------------------------------------------------------

            print(
                f"[{unidade_nome}] URL atual: {page.url}"
            )

            arquivo.write(
                f"URL ATUAL:\n{page.url}\n\n"
            )

            # ------------------------------------------------------------------
            # TEXTO VISÍVEL
            # ------------------------------------------------------------------

            print(
                f"[{unidade_nome}] "
                f"Analisando texto visível..."
            )

            try:

                texto_body = page.locator(
                    "body"
                ).inner_text(
                    timeout=10000
                )

                arquivo.write(
                    "===============================================================================\n"
                )

                arquivo.write(
                    "TEXTO VISÍVEL DA PÁGINA\n"
                )

                arquivo.write(
                    "===============================================================================\n"
                )

                arquivo.write(
                    texto_body[:30000]
                )

                arquivo.write("\n\n")

            except Exception as erro:

                arquivo.write(
                    f"Erro ao obter body: {erro}\n\n"
                )

            # ------------------------------------------------------------------
            # LINKS
            # ------------------------------------------------------------------

            print(
                f"[{unidade_nome}] "
                f"Analisando links..."
            )

            arquivo.write(
                "===============================================================================\n"
            )

            arquivo.write(
                "LINKS ENCONTRADOS\n"
            )

            arquivo.write(
                "===============================================================================\n"
            )

            links = page.locator("a")

            quantidade_links = links.count()

            print(
                f"[{unidade_nome}] "
                f"Total de links: {quantidade_links}"
            )

            for i in range(quantidade_links):

                try:

                    link = links.nth(i)

                    if not link.is_visible():
                        continue

                    texto = (
                        link.inner_text(
                            timeout=2000
                        )
                        .strip()
                    )

                    href = (
                        link.get_attribute("href")
                        or ""
                    )

                    title = (
                        link.get_attribute("title")
                        or ""
                    )

                    aria = (
                        link.get_attribute("aria-label")
                        or ""
                    )

                    linha = (
                        f"[LINK {i}] "
                        f"texto={texto!r} | "
                        f"href={href!r} | "
                        f"title={title!r} | "
                        f"aria-label={aria!r}"
                    )

                    arquivo.write(
                        linha + "\n"
                    )

                    texto_busca = (
                        f"{texto} "
                        f"{href} "
                        f"{title} "
                        f"{aria}"
                    ).lower()

                    if any(
                        palavra in texto_busca
                        for palavra in palavras_interesse
                    ):

                        print(
                            f"[{unidade_nome}] "
                            f"LINK DE INTERESSE: {linha}"
                        )

                except Exception as erro:

                    arquivo.write(
                        f"[LINK {i}] erro: {erro}\n"
                    )

            arquivo.write("\n")

            # ------------------------------------------------------------------
            # BOTÕES
            # ------------------------------------------------------------------

            print(
                f"[{unidade_nome}] "
                f"Analisando botões..."
            )

            arquivo.write(
                "===============================================================================\n"
            )

            arquivo.write(
                "BOTÕES ENCONTRADOS\n"
            )

            arquivo.write(
                "===============================================================================\n"
            )

            botoes = page.locator("button")

            quantidade_botoes = botoes.count()

            print(
                f"[{unidade_nome}] "
                f"Total de botões: {quantidade_botoes}"
            )

            for i in range(quantidade_botoes):

                try:

                    botao = botoes.nth(i)

                    if not botao.is_visible():
                        continue

                    texto = (
                        botao.inner_text(
                            timeout=2000
                        )
                        .strip()
                    )

                    title = (
                        botao.get_attribute("title")
                        or ""
                    )

                    aria = (
                        botao.get_attribute("aria-label")
                        or ""
                    )

                    tipo = (
                        botao.get_attribute("type")
                        or ""
                    )

                    classes = (
                        botao.get_attribute("class")
                        or ""
                    )

                    linha = (
                        f"[BUTTON {i}] "
                        f"texto={texto!r} | "
                        f"title={title!r} | "
                        f"aria-label={aria!r} | "
                        f"type={tipo!r} | "
                        f"class={classes!r}"
                    )

                    arquivo.write(
                        linha + "\n"
                    )

                    texto_busca = (
                        f"{texto} "
                        f"{title} "
                        f"{aria} "
                        f"{classes}"
                    ).lower()

                    if any(
                        palavra in texto_busca
                        for palavra in palavras_interesse
                    ):

                        print(
                            f"[{unidade_nome}] "
                            f"BOTÃO DE INTERESSE: {linha}"
                        )

                except Exception as erro:

                    arquivo.write(
                        f"[BUTTON {i}] erro: {erro}\n"
                    )

            arquivo.write("\n")

            # ------------------------------------------------------------------
            # ELEMENTOS ROLE=MENUITEM
            # ------------------------------------------------------------------

            print(
                f"[{unidade_nome}] "
                f"Analisando elementos de menu..."
            )

            arquivo.write(
                "===============================================================================\n"
            )

            arquivo.write(
                "ELEMENTOS ROLE=MENUITEM\n"
            )

            arquivo.write(
                "===============================================================================\n"
            )

            menuitems = page.locator(
                '[role="menuitem"]'
            )

            quantidade_menuitems = menuitems.count()

            print(
                f"[{unidade_nome}] "
                f"Menuitems: {quantidade_menuitems}"
            )

            for i in range(quantidade_menuitems):

                try:

                    item = menuitems.nth(i)

                    if not item.is_visible():
                        continue

                    texto = (
                        item.inner_text(
                            timeout=2000
                        )
                        .strip()
                    )

                    aria = (
                        item.get_attribute("aria-label")
                        or ""
                    )

                    href = (
                        item.get_attribute("href")
                        or ""
                    )

                    linha = (
                        f"[MENUITEM {i}] "
                        f"texto={texto!r} | "
                        f"aria-label={aria!r} | "
                        f"href={href!r}"
                    )

                    arquivo.write(
                        linha + "\n"
                    )

                    texto_busca = (
                        f"{texto} "
                        f"{aria} "
                        f"{href}"
                    ).lower()

                    if any(
                        palavra in texto_busca
                        for palavra in palavras_interesse
                    ):

                        print(
                            f"[{unidade_nome}] "
                            f"MENU DE INTERESSE: {linha}"
                        )

                except Exception as erro:

                    arquivo.write(
                        f"[MENUITEM {i}] erro: {erro}\n"
                    )

            arquivo.write("\n")

            # ------------------------------------------------------------------
            # ELEMENTOS COM TEXTO DE INTERESSE
            # ------------------------------------------------------------------

            print(
                f"[{unidade_nome}] "
                f"Procurando elementos relacionados "
                f"a faltas/frequência..."
            )

            arquivo.write(
                "===============================================================================\n"
            )

            arquivo.write(
                "ELEMENTOS COM PALAVRAS-CHAVE DE INTERESSE\n"
            )

            arquivo.write(
                "===============================================================================\n"
            )

            encontrados = []

            seletores_textuais = [
                "a",
                "button",
                "[role='menuitem']",
                "[role='button']",
                "li",
                "span",
                "div",
                "td",
                "th",
                "label",
            ]

            for seletor in seletores_textuais:

                try:

                    elementos = page.locator(
                        seletor
                    )

                    quantidade = elementos.count()

                    for i in range(
                        min(quantidade, 1000)
                    ):

                        try:

                            elemento = elementos.nth(i)

                            if not elemento.is_visible():
                                continue

                            texto = (
                                elemento.inner_text(
                                    timeout=1000
                                )
                                .strip()
                            )

                            if not texto:
                                continue

                            texto_limpo = (
                                " ".join(
                                    texto.split()
                                )
                            )

                            if len(texto_limpo) > 300:
                                continue

                            texto_busca = (
                                texto_limpo.lower()
                            )

                            palavras_encontradas = [
                                palavra
                                for palavra in palavras_interesse
                                if palavra in texto_busca
                            ]

                            if palavras_encontradas:

                                chave = (
                                    seletor,
                                    texto_limpo,
                                )

                                if chave not in encontrados:

                                    encontrados.append(
                                        chave
                                    )

                                    linha = (
                                        f"seletor={seletor} | "
                                        f"palavras={palavras_encontradas} | "
                                        f"texto={texto_limpo!r}"
                                    )

                                    arquivo.write(
                                        linha + "\n"
                                    )

                        except Exception:
                            pass

                except Exception:
                    pass

            print(
                f"[{unidade_nome}] "
                f"Elementos de interesse encontrados: "
                f"{len(encontrados)}"
            )

            arquivo.write("\n")

            # ------------------------------------------------------------------
            # SELECTS
            # ------------------------------------------------------------------

            arquivo.write(
                "===============================================================================\n"
            )

            arquivo.write(
                "SELECTS ENCONTRADOS\n"
            )

            arquivo.write(
                "===============================================================================\n"
            )

            selects = page.locator("select")

            quantidade_selects = selects.count()

            print(
                f"[{unidade_nome}] "
                f"Selects: {quantidade_selects}"
            )

            for i in range(quantidade_selects):

                try:

                    select = selects.nth(i)

                    if not select.is_visible():
                        continue

                    nome = (
                        select.get_attribute("name")
                        or ""
                    )

                    identificador = (
                        select.get_attribute("id")
                        or ""
                    )

                    options = select.locator(
                        "option"
                    )

                    quantidade_options = options.count()

                    arquivo.write(
                        f"[SELECT {i}] "
                        f"name={nome!r} "
                        f"id={identificador!r} "
                        f"options={quantidade_options}\n"
                    )

                    for j in range(
                        min(quantidade_options, 100)
                    ):

                        try:

                            option = options.nth(j)

                            texto_option = (
                                option.inner_text(
                                    timeout=1000
                                )
                                .strip()
                            )

                            valor_option = (
                                option.get_attribute(
                                    "value"
                                )
                                or ""
                            )

                            arquivo.write(
                                f"    [{j}] "
                                f"text={texto_option!r} "
                                f"value={valor_option!r}\n"
                            )

                        except Exception:
                            pass

                except Exception as erro:

                    arquivo.write(
                        f"[SELECT {i}] erro: {erro}\n"
                    )

            arquivo.write("\n")

            # ------------------------------------------------------------------
            # IFRAMES
            # ------------------------------------------------------------------

            arquivo.write(
                "===============================================================================\n"
            )

            arquivo.write(
                "IFRAMES\n"
            )

            arquivo.write(
                "===============================================================================\n"
            )

            for i, frame in enumerate(page.frames):

                try:

                    arquivo.write(
                        f"[FRAME {i}] "
                        f"url={frame.url}\n"
                    )

                    try:

                        frame_texto = frame.locator(
                            "body"
                        ).inner_text(
                            timeout=3000
                        )

                        frame_texto = (
                            frame_texto[:10000]
                        )

                        arquivo.write(
                            frame_texto + "\n"
                        )

                    except Exception:
                        pass

                except Exception:
                    pass

            arquivo.write("\n")

            # ------------------------------------------------------------------
            # TABELAS
            # ------------------------------------------------------------------

            arquivo.write(
                "===============================================================================\n"
            )

            arquivo.write(
                "TABELAS\n"
            )

            arquivo.write(
                "===============================================================================\n"
            )

            tabelas = page.locator("table")

            quantidade_tabelas = tabelas.count()

            print(
                f"[{unidade_nome}] "
                f"Tabelas: {quantidade_tabelas}"
            )

            for i in range(quantidade_tabelas):

                try:

                    tabela = tabelas.nth(i)

                    if not tabela.is_visible():
                        continue

                    arquivo.write(
                        f"\n--- TABELA {i} ---\n"
                    )

                    headers = tabela.locator(
                        "thead th"
                    ).all_text_contents()

                    if not headers:

                        headers = tabela.locator(
                            "tr:first-child th, "
                            "tr:first-child td"
                        ).all_text_contents()

                    headers = [
                        " ".join(
                            texto.split()
                        )
                        for texto in headers
                    ]

                    arquivo.write(
                        f"CABEÇALHOS: {headers}\n"
                    )

                    linhas = tabela.locator(
                        "tbody tr"
                    )

                    quantidade_linhas = linhas.count()

                    arquivo.write(
                        f"LINHAS: {quantidade_linhas}\n"
                    )

                    for j in range(
                        min(quantidade_linhas, 50)
                    ):

                        try:

                            colunas = linhas.nth(j).locator(
                                "td"
                            ).all_text_contents()

                            colunas = [
                                " ".join(
                                    coluna.split()
                                )
                                for coluna in colunas
                            ]

                            arquivo.write(
                                f"[{j}] {colunas}\n"
                            )

                        except Exception:
                            pass

                except Exception as erro:

                    arquivo.write(
                        f"TABELA {i} - erro: {erro}\n"
                    )

            arquivo.write("\n")

            # ------------------------------------------------------------------
            # PALAVRAS-CHAVE NO HTML
            # ------------------------------------------------------------------

            arquivo.write(
                "===============================================================================\n"
            )

            arquivo.write(
                "PALAVRAS-CHAVE ENCONTRADAS NO HTML\n"
            )

            arquivo.write(
                "===============================================================================\n"
            )

            try:

                html = page.content()

                html_lower = html.lower()

                for palavra in palavras_interesse:

                    quantidade = html_lower.count(
                        palavra.lower()
                    )

                    arquivo.write(
                        f"{palavra}: {quantidade} ocorrência(s)\n"
                    )

            except Exception as erro:

                arquivo.write(
                    f"Erro analisando HTML: {erro}\n"
                )

        print("")
        print(
            f"[{unidade_nome}] "
            f"DIAGNÓSTICO 3A concluído."
        )

        print(
            f"[{unidade_nome}] "
            f"Arquivo principal:"
        )

        print(
            str(diagnostico_path)
        )

        print("=" * 80)
        print("")

    except Exception as erro:

        print(
            f"[{unidade_nome}] "
            f"Erro no diagnóstico de navegação: {erro}"
        )


# ==============================================================================
# ESTADO DA PÁGINA
# ==============================================================================

def imprimir_estado_pagina(page, unidade_nome, etapa):

    print("")
    print("=" * 80)
    print(
        f"DIAGNÓSTICO DA PÁGINA - {unidade_nome}"
    )
    print(
        f"ETAPA: {etapa}"
    )
    print("=" * 80)

    try:
        print(
            f"URL atual: {page.url}"
        )
    except Exception:
        print(
            "URL atual: não disponível"
        )

    try:
        print(
            f"Título: {page.title()}"
        )
    except Exception:
        print(
            "Título: não disponível"
        )

    try:
        print(
            f"Quantidade de inputs: "
            f"{page.locator('input').count()}"
        )
    except Exception:
        pass

    try:
        print(
            f"Quantidade de botões: "
            f"{page.locator('button').count()}"
        )
    except Exception:
        pass

    try:
        print(
            f"Quantidade de links: "
            f"{page.locator('a').count()}"
        )
    except Exception:
        pass

    try:
        print(
            f"Quantidade de frames: "
            f"{len(page.frames)}"
        )
    except Exception:
        pass

    print("")
    print(
        "Texto visível inicial da página:"
    )

    try:

        texto = page.locator(
            "body"
        ).inner_text(
            timeout=10000
        )

        texto = texto[:5000]

        print(texto)

    except Exception as erro:

        print(
            f"Não foi possível obter texto da página: "
            f"{erro}"
        )

    print("=" * 80)
    print("")


# ==============================================================================
# LOGIN
# ==============================================================================

def verificar_elementos_login(page):

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

    print(
        "Verificando campos de login..."
    )

    login_encontrado = None

    for seletor in seletores_login:

        try:

            locator = page.locator(
                seletor
            )

            quantidade = locator.count()

            print(
                f"LOGIN selector: {seletor} "
                f"=> {quantidade}"
            )

            if quantidade > 0:

                for i in range(quantidade):

                    try:

                        if locator.nth(i).is_visible():

                            login_encontrado = (
                                locator.nth(i)
                            )

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
    print(
        "Verificando campo de senha..."
    )

    for seletor in seletores_senha:

        try:

            locator = page.locator(
                seletor
            )

            quantidade = locator.count()

            print(
                f"SENHA selector: {seletor} "
                f"=> {quantidade}"
            )

            if quantidade > 0:

                for i in range(quantidade):

                    try:

                        if locator.nth(i).is_visible():

                            senha_encontrada = (
                                locator.nth(i)
                            )

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

    return (
        login_encontrado,
        senha_encontrada
    )


def localizar_botao_login(page):

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

            locator = page.locator(
                seletor
            )

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
    """

    if not data_inicio_str:
        return "normal", 0

    try:

        data_inicio_str = (
            data_inicio_str.strip()
        )

        data_inicio = datetime.strptime(
            data_inicio_str,
            "%d/%m/%Y"
        )

        hoje = datetime.now()

        dias = (
            hoje - data_inicio
        ).days

        if dias >= 90:

            return "critico", dias

        elif dias >= 60:

            return "moderado", dias

        elif dias >= 30:

            return "atencao", dias

        else:

            return "normal", max(
                0,
                dias
            )

    except Exception:

        return "normal", 0


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
    print(
        f"INICIANDO PROCESSAMENTO: {unidade_nome}"
    )
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

        login_input, senha_input = (
            verificar_elementos_login(page)
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

            login_input.fill(
                usuario
            )

            senha_input.fill(
                senha
            )

            botao = localizar_botao_login(
                page
            )

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

            page.wait_for_timeout(
                5000
            )

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

            # ==================================================================
            # ETAPA 3A
            # ==================================================================

            diagnosticar_navegacao_cgd(
                page,
                unidade_nome
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
                f"Não vamos assumir que já está autenticado."
            )

            salvar_diagnostico(
                page,
                unidade_nome,
                "login_nao_encontrado"
            )

            # ------------------------------------------------------------------
            # TENTATIVA DE DIAGNÓSTICO MESMO SEM LOGIN
            # ------------------------------------------------------------------

            diagnosticar_navegacao_cgd(
                page,
                unidade_nome
            )

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

        page.wait_for_timeout(
            5000
        )

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
        # 5. VERIFICAR TABELA
        # ----------------------------------------------------------------------

        quantidade_tabelas = (
            page.locator("table").count()
        )

        quantidade_linhas = (
            page.locator(
                "table tbody tr"
            ).count()
        )

        print("")
        print(
            f"[{unidade_nome}] "
            f"Tabelas encontradas: "
            f"{quantidade_tabelas}"
        )

        print(
            f"[{unidade_nome}] "
            f"Linhas encontradas: "
            f"{quantidade_linhas}"
        )

        if quantidade_tabelas == 0:

            print("")
            print(
                f"[{unidade_nome}] "
                f"ERRO: nenhuma tabela encontrada."
            )

            salvar_diagnostico(
                page,
                unidade_nome,
                "sem_tabela"
            )

            return alunos_capturados

        # ----------------------------------------------------------------------
        # 6. TENTAR AUMENTAR REGISTROS
        # ----------------------------------------------------------------------

        try:

            select_limit = page.locator(
                'select[name*="length" i], '
                'select[name*="limit" i], '
                'select[name*="per_page" i]'
            ).first

            if select_limit.is_visible(
                timeout=3000
            ):

                print(
                    f"[{unidade_nome}] "
                    f"Controle de quantidade por página encontrado."
                )

                try:

                    select_limit.select_option(
                        value="-1"
                    )

                    page.wait_for_timeout(
                        3000
                    )

                except Exception as erro:

                    print(
                        f"[{unidade_nome}] "
                        f"Não foi possível selecionar -1: "
                        f"{erro}"
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

            page.wait_for_timeout(
                2000
            )

            rows = page.locator(
                "table tbody tr"
            ).all()

            print(
                f"[{unidade_nome}] "
                f"Linhas encontradas nesta página: "
                f"{len(rows)}"
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

                    curso_texto = (
                        curso.lower()
                    )

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

                    # ----------------------------------------------------------
                    # ETAPA 2B - UNIDADE
                    # ----------------------------------------------------------

                    unidade_db = (
                        "matriz"
                        if unidade_nome.lower() == "matriz"
                        else "filial"
                    )

                    # ----------------------------------------------------------
                    # ETAPA 2B - CRITICIDADE
                    # ----------------------------------------------------------

                    criticidade_db = (
                        criticidade.lower()
                    )

                    if criticidade_db == "critico":

                        tratativa_sugerida = (
                            "aulao"
                        )

                    elif criticidade_db == "moderado":

                        tratativa_sugerida = (
                            "atividade_pratica"
                        )

                    elif criticidade_db == "atencao":

                        tratativa_sugerida = (
                            "acompanhamento"
                        )

                    else:

                        tratativa_sugerida = (
                            "normal"
                        )

                    # ----------------------------------------------------------
                    # DATA PARA SUPABASE
                    # ----------------------------------------------------------

                    if data_inicio_str:

                        try:

                            data_inicio_db = (
                                datetime.strptime(
                                    data_inicio_str,
                                    "%d/%m/%Y"
                                ).strftime(
                                    "%Y-%m-%d"
                                )
                            )

                        except Exception:

                            data_inicio_db = (
                                datetime.now().strftime(
                                    "%Y-%m-%d"
                                )
                            )

                    else:

                        data_inicio_db = (
                            datetime.now().strftime(
                                "%Y-%m-%d"
                            )
                        )

                    # ----------------------------------------------------------
                    # ALUNO
                    # ----------------------------------------------------------

                    aluno_data = {

                        "cgd_matricula_id": contrato,

                        "nome": nome,

                        "contrato": contrato,

                        "email": None,

                        "telefone": None,

                        "curso": curso,

                        "turma_nome": "",

                        "professor_nome": "",

                        "data_inicio": data_inicio_db,

                        "meses_contrato_total": 12,

                        "ultima_aula": None,

                        "ultimo_acesso": None,

                        # ------------------------------------------------------
                        # ETAPA 3A AINDA NÃO ALTERA FALTAS
                        # ------------------------------------------------------

                        "faltas_totais": 0,

                        "faltas_mes_atual": 0,

                        "mes_referencia_faltas": (
                            datetime.now().strftime(
                                "%m/%Y"
                            )
                        ),

                        "dias_em_curso": dias_ativos,

                        "criticidade": criticidade_db,

                        "tratativa_sugerida": (
                            tratativa_sugerida
                        ),

                        "status_tratativa": (
                            "pendente"
                        ),

                        "status_matricula": (
                            "ativo"
                        ),

                        "bloqueado_automaticamente": (
                            False
                        ),

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
                        f"{indice + 1}: "
                        f"{erro_linha}"
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

                if not btn_proximo.is_visible(
                    timeout=3000
                ):

                    print(
                        f"[{unidade_nome}] "
                        f"Botão de próxima página não encontrado."
                    )

                    break

                classes = (
                    btn_proximo.get_attribute(
                        "class"
                    )
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

                page.wait_for_timeout(
                    3000
                )

                pagina_atual += 1

                if pagina_atual > 100:

                    print(
                        f"[{unidade_nome}] "
                        f"Proteção ativada: "
                        f"mais de 100 páginas."
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
    print(
        "Verificação das variáveis de ambiente:"
    )

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
        print(
            "Iniciando Chromium..."
        )

        browser = p.chromium.launch(
            headless=True
        )

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

            page_matriz = (
                context_matriz.new_page()
            )

            alunos_matriz = (
                fazer_login_e_extrair(
                    page_matriz,
                    LOGIN_MATRIZ,
                    SENHA_MATRIZ,
                    URL_MATRIZ,
                    "Matriz"
                )
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

            page_filial = (
                context_filial.new_page()
            )

            alunos_filial = (
                fazer_login_e_extrair(
                    page_filial,
                    LOGIN_FILIAL,
                    SENHA_FILIAL,
                    URL_FILIAL,
                    "Filial"
                )
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
                "Enviando alunos estruturados para o Supabase..."
            )

            supabase: Client = create_client(
                SUPABASE_URL,
                SUPABASE_KEY
            )

            # ------------------------------------------------------------------
            # 1. UPSERT DOS ALUNOS
            # ------------------------------------------------------------------

            if todos_alunos:

                resposta_alunos = (
                    supabase
                    .table("alunos")
                    .upsert(
                        todos_alunos,
                        on_conflict="cgd_matricula_id"
                    )
                    .execute()
                )

                print(
                    "Alunos enviados com sucesso "
                    "para a tabela alunos."
                )

                print(
                    f"Quantidade enviada: "
                    f"{len(todos_alunos)}"
                )

            # ------------------------------------------------------------------
            # 2. RESUMO POR UNIDADE
            # ------------------------------------------------------------------

            for unidade in [
                "matriz",
                "filial"
            ]:

                alunos_unidade = [
                    aluno
                    for aluno in todos_alunos
                    if aluno["unidade"] == unidade
                ]

                total_alunos = len(
                    alunos_unidade
                )

                criticos = sum(
                    1
                    for aluno in alunos_unidade
                    if aluno["criticidade"] == "critico"
                )

                moderados = sum(
                    1
                    for aluno in alunos_unidade
                    if aluno["criticidade"] == "moderado"
                )

                atencao = sum(
                    1
                    for aluno in alunos_unidade
                    if aluno["criticidade"] == "atencao"
                )

                normais = sum(
                    1
                    for aluno in alunos_unidade
                    if aluno["criticidade"] == "normal"
                )

                bloqueados = sum(
                    1
                    for aluno in alunos_unidade
                    if aluno["bloqueado_automaticamente"]
                )

                resumo = {

                    "unidade": unidade,

                    "nome_unidade": (
                        "Matriz"
                        if unidade == "matriz"
                        else "Filial"
                    ),

                    "total_alunos_ativos": (
                        total_alunos
                    ),

                    "total_matriz": (
                        total_alunos
                        if unidade == "matriz"
                        else 0
                    ),

                    "total_filial": (
                        total_alunos
                        if unidade == "filial"
                        else 0
                    ),

                    "alunos_criticos": criticos,

                    "alunos_moderados": moderados,

                    "total_contratos": total_alunos,

                    "laboratorios_ativos": [],

                    "criticos": criticos,

                    "moderados": moderados,

                    "atencao": atencao,

                    "normais": normais,

                    "bloqueados_faltas": bloqueados,

                    "mes_referencia": (
                        datetime.now().strftime(
                            "%m/%Y"
                        )
                    ),

                    "alunos_data": alunos_unidade,

                    "origem": "cgd_live",

                    "ultimo_sync": (
                        datetime.now().isoformat()
                    )
                }

                resposta_resumo = (
                    supabase
                    .table("resumo_cgd")
                    .upsert(
                        resumo,
                        on_conflict="unidade"
                    )
                    .execute()
                )

                print(
                    f"Resumo da unidade {unidade} "
                    f"atualizado com sucesso."
                )

        except Exception as erro_supabase:

            print("")
            print(
                "ERRO AO ATUALIZAR SUPABASE:"
            )

            print(
                erro_supabase
            )


# ==============================================================================
# EXECUÇÃO
# ==============================================================================

if __name__ == "__main__":
    main()
