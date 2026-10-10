"""Compatibility import for legacy workflow steps.

Playwright's native Chromium headless mode is used directly. This module must
not monkey-patch BrowserType.launch, force a visible window, or add window
minimization flags.
"""
try:
    import frequency_runtime_patch  # noqa: F401
except Exception as exc:
    raise RuntimeError(f"PATCH_FREQUENCIA_LISTA_ERRO={type(exc).__name__}: {exc}") from exc
print("PATCH_BROWSER_NATIVE_HEADLESS=OK", flush=True)
