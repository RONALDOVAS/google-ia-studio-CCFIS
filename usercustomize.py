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

    def _login_fields(page):
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
        for frame in list(page.frames):
            p = _first(frame, password_selectors)
            if not p:
                continue
            u = _first(frame, username_selectors)
            if not u:
                try:
                    inputs = frame.locator("input")
                    for i in range(inputs.count()):
                        item = inputs.nth(i)
                        if item.is_visible() and item.is_enabled():
                            kind = (item.get_attribute("type") or "text").lower()
                            if kind in ("text", "email"):
                                u = item
                                break
                except Exception:
                    pass
            if u:
                return u, p, frame
        return None, None, None

    def _login_diagnostic(page, unidade, reason):
        from pathlib import Path
        import re
        from urllib.parse import urlparse

        root = Path(__file__).resolve().parent / "diagnostico_scraping"
        root.mkdir(parents=True, exist_ok=True)
        safe_unit = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(unidade))
        try:
            title = page.title()
        except Exception:
            title = ""
        try:
            body = page.locator("body").inner_text(timeout=3000)
        except Exception:
            body = ""
        # Nunca registrar credenciais, cookies ou query strings na saída do log.
        parsed = urlparse(page.url)
        safe_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        try:
            (root / f"login_{safe_unit}.txt").write_text(
                f"reason={reason}\nurl={safe_url}\ntitle={title[:500]}\n"
                f"body_text={body[:12000]}\nframes={len(page.frames)}\n",
                encoding="utf-8",
            )
        except Exception:
            pass
        try:
            (root / f"login_{safe_unit}.html").write_text(page.content(), encoding="utf-8")
        except Exception:
            pass
        try:
            page.screenshot(path=str(root / f"login_{safe_unit}.png"), full_page=True, timeout=10000)
        except Exception:
            pass
        low = (title + "\n" + body).lower()
        if any(x in low for x in ("cloudflare", "checking your browser", "just a moment",
                                  "you have been blocked", "challenge-platform", "cf-chl-")):
            category = "PAGINA_DE_DESAFIO_O_BLOQUEIO"
        elif not body.strip():
            category = "PAGINA_VAZIA_O_SEM_CONTEUDO"
        else:
            category = "FORMULARIO_NAO_RENDERIZADO_OU_URL_INCORRETA"
        return category, safe_url, title[:180]

    def login(page, user, password, unidade):
        from urllib.parse import urlparse, urlunparse

        if not user or not password:
            raise RuntimeError(f"[{unidade}] CREDENCIAIS_NAO_CONFIGURADAS")

        configured = scraper.CGD_LOGIN_URL
        parsed = urlparse(configured)
        candidates = [configured]
        # Só tenta rotas alternativas no host oficial já configurado para o CGD.
        if scraper.same_host(configured):
            origin = f"{parsed.scheme}://{parsed.netloc}"
            for candidate in (origin + "/login", origin + "/"):
                if candidate not in candidates:
                    candidates.append(candidate)

        username = senha = frame_login = None
        attempts = []
        for target in candidates:
            try:
                page.goto(target, wait_until="domcontentloaded", timeout=scraper.PAGE_TIMEOUT_MS)
            except Exception as exc:
                attempts.append(f"navigation_error={type(exc).__name__}")
                continue

            # Aguarda hidratação do frontend sem depender de networkidle, que pode
            # nunca ocorrer em aplicações com chamadas long-polling.
            try:
                page.wait_for_timeout(1200)
                page.wait_for_function(
                    "() => document.querySelectorAll('input').length > 0",
                    timeout=7000,
                )
            except Exception:
                pass
            username, senha, frame_login = _login_fields(page)
            if username and senha:
                print(f"[{unidade}] FORMULARIO_LOGIN_ENCONTRADO rota={urlparse(page.url).path}", flush=True)
                break
            attempts.append(f"{urlparse(page.url).netloc}{urlparse(page.url).path}:frames={len(page.frames)}")

        if not username or not senha:
            category, safe_url, title = _login_diagnostic(page, unidade, ";".join(attempts))
            raise RuntimeError(
                f"[{unidade}] {category}: formulario nao renderizado; "
                f"url={safe_url}; title={title!r}; tentativas={attempts}. "
                "Ver diagnostico_scraping/login_<unidade>.(txt|png|html) no artefato."
            )

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
        page.wait_for_timeout(2500)
        final_path = urlparse(page.url).path.rstrip("/").lower()
        if final_path == "/login" or final_path.startswith("/login/"):
            category, safe_url, title = _login_diagnostic(page, unidade, "formulario_submetido_mas_sessao_nao_confirmada")
            raise RuntimeError(
                f"[{unidade}] LOGIN_NAO_CONFIRMADO: url={safe_url}; title={title!r}; "
                f"diagnostico={category}. Ver artefato."
            )
        if not scraper.same_host(page.url):
            raise RuntimeError(f"[{unidade}] LOGIN_SAIU_DO_HOST_CGD: {urlparse(page.url).netloc}")
        print(f"[{unidade}] LOGIN OK (usercustomize): {urlparse(page.url).path}", flush=True)

    scraper.login = login


_patch()
