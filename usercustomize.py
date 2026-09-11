"""Fallback de autenticacao CGD carregado pelo Python site module.

O login publico do CGD apresenta E-mail, Senha e Entrar. Em alguns ambientes
Playwright pode receber a pagina em um frame ou em estado ainda nao hidratado.
Este patch procura os campos em todas as frames e nao depende de name/id.
"""
from urllib.parse import urlparse


def _first(frame, selectors):
    for selector in selectors:
        try:
            loc = frame.locator(selector)
            for i in range(loc.count()):
                item = loc.nth(i)
                if item.is_visible() and item.is_enabled():
                    return item
        except Exception:
            pass
    return None


def _patch():
    try:
        import scraper
    except Exception:
        return

    def login(page, user, password, unidade):
        if not user or not password:
            raise RuntimeError(f"[{unidade}] CREDENCIAIS_NAO_CONFIGURADAS")
        page.goto(scraper.CGD_LOGIN_URL, wait_until="domcontentloaded", timeout=scraper.PAGE_TIMEOUT_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=10000)
        except Exception:
            pass
        page.wait_for_timeout(1800)

        username_selectors = [
            'input[autocomplete="username"]', 'input[autocomplete="email"]',
            'input[type="email"]', 'input[placeholder*="e-mail" i]',
            'input[placeholder*="email" i]', 'input[placeholder*="usuário" i]',
            'input[placeholder*="usuario" i]', 'input[aria-label*="e-mail" i]',
            'input[aria-label*="email" i]', 'input[name*="email" i]',
            'input[name*="user" i]', 'input[name*="login" i]',
        ]
        password_selectors = [
            'input[autocomplete="current-password"]', 'input[type="password"]',
            'input[placeholder*="senha" i]', 'input[placeholder*="password" i]',
            'input[aria-label*="senha" i]', 'input[aria-label*="password" i]',
            'input[name*="senha" i]', 'input[name*="password" i]',
        ]

        username = senha = frame_login = None
        for frame in list(page.frames):
            p = _first(frame, password_selectors)
            if not p:
                continue
            u = _first(frame, username_selectors)
            if not u:
                try:
                    inputs = frame.locator('input')
                    for i in range(inputs.count()):
                        x = inputs.nth(i)
                        if x.is_visible() and x.is_enabled() and (x.get_attribute('type') or 'text').lower() in ('text','email'):
                            u = x
                            break
                except Exception:
                    pass
            if u:
                username, senha, frame_login = u, p, frame
                break

        if not username or not senha:
            info = []
            for frame in list(page.frames):
                try:
                    input_count = frame.locator('input').count()
                    password_count = frame.locator('input[type="password"]').count()
                    info.append(f"{frame.url}:inputs={input_count}:password={password_count}")
                except Exception:
                    info.append(frame.url)
            raise RuntimeError(f"[{unidade}] CAMPOS_LOGIN_NAO_ENCONTRADOS: {page.url} frames={info}")

        username.fill(user)
        senha.fill(password)
        button = _first(frame_login, [
            'button[type="submit"]', 'input[type="submit"]',
            'button:has-text("Entrar")', 'button:has-text("Acessar")',
            'button:has-text("Login")', 'button:has-text("Continuar")',
        ])
        if button:
            button.click()
        else:
            senha.press("Enter")

        try:
            page.wait_for_load_state("domcontentloaded", timeout=15000)
        except Exception:
            pass
        page.wait_for_timeout(3000)
        final_path = urlparse(page.url).path.rstrip('/').lower()
        if final_path == '/login' or final_path.startswith('/login/'):
            page.wait_for_timeout(3000)
            final_path = urlparse(page.url).path.rstrip('/').lower()
        if final_path == '/login' or final_path.startswith('/login/'):
            raise RuntimeError(f"[{unidade}] LOGIN_REJEITADO_OU_SESSAO_NAO_ESTABELECIDA: {page.url}")
        if not scraper.same_host(page.url):
            raise RuntimeError(f"[{unidade}] LOGIN_SAIU_DO_HOST_CGD: {page.url}")
        print(f"[{unidade}] LOGIN OK (usercustomize): {page.url}", flush=True)

    scraper.login = login


_patch()
