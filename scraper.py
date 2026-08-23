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

        login_url = "https://app.cgd.com.br"
        # COLOQUE AQUI A URL EXATA DA SUA ABA "Alunos - CGD Gestão"
        alunos_url = "https://app.cgd.com.br/alunos" 

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
            print(f"Login efetuado. URL atual: {page.url}")
        except Exception as err:
            print(f"Aviso no login: {err}")

        # Tenta navegar diretamente para a URL de alunos
        print(f"Navegando diretamente para a URL de alunos: {alunos_url}")
        page.goto(alunos_url, wait_until="domcontentloaded", timeout=25000)
        page.wait_for_timeout(3000)

        # Se mesmo assim redirecionar para a home, tenta clicar fisicamente no menu Alunos
        if page.url.strip("/") == "https://app.cgd.com.br":
            print("Redirecionou para home. Tentando clicar no menu 'Alunos' da interface...")
            try:
                page.locator('a:has-text("Alunos"), .nav-link:has-text("Alunos")').first.click()
                page.wait_for_timeout(3000)
            except Exception as e:
                print(f"Erro ao tentar clicar no menu: {e}")

        alunos = extrair_alunos_da_tabela(page, "Matriz")
        browser.close()
        return alunos
