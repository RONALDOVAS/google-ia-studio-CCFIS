"""Runtime patches for CGD automation on the self-hosted Windows runner."""
from playwright.sync_api import BrowserType

_original_launch = BrowserType.launch


def _launch_offscreen(self, *args, **kwargs):
    launch_args = list(kwargs.pop("args", None) or [])
    required = [
        "--start-minimized",
        "--window-position=-32000,-32000",
        "--disable-background-networking",
    ]
    for flag in required:
        if flag not in launch_args:
            launch_args.append(flag)
    kwargs["headless"] = False
    kwargs["args"] = launch_args
    return _original_launch(self, *args, **kwargs)


BrowserType.launch = _launch_offscreen
print("PATCH_BROWSER_OFFSCREEN=OK", flush=True)

# Integra a captura coletiva na mesma sessao autenticada do sincronizador.
# O patch e aplicado antes da importacao/execucao do scraper principal.
try:
    import scraper
    from frequencia_coletiva_cgd import capture_and_persist

    _original_login = scraper.login

    def _login_with_collective_capture(page, user, password, unidade):
        result = _original_login(page, user, password, unidade)
        try:
            capture_and_persist(page, unidade)
        except Exception as exc:
            print(f"[{unidade}] FREQUENCIA_COLETIVA_ERRO={exc!r}", flush=True)
        return result

    scraper.login = _login_with_collective_capture
    print("PATCH_FREQUENCIA_COLETIVA_SESSAO=OK", flush=True)
except Exception as exc:
    print(f"PATCH_FREQUENCIA_COLETIVA_SESSAO_ERRO={exc!r}", flush=True)

try:
    import frequency_runtime_patch  # noqa: F401
except Exception as exc:
    print(f"PATCH_FREQUENCIA_LISTA_ERRO={exc!r}", flush=True)
else:
    print("PATCH_FREQUENCIA_LISTA=OK", flush=True)
