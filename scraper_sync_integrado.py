"""Ponto de entrada integrado do sincronizador CGD.

Mantem o fluxo principal existente e injeta a captura coletiva na mesma pagina
ja autenticada. A captura e persistida separadamente para auditoria, sem
abrir outro navegador ou repetir login.
"""
from functools import wraps
import runpy

import scraper
from frequencia_coletiva_cgd import capture_and_persist

_original_login = scraper.login


def login_with_collective_capture(page, user, password, unidade):
    result = _original_login(page, user, password, unidade)
    try:
        capture_and_persist(page, unidade)
    except Exception as exc:
        print(f"[{unidade}] FREQUENCIA_COLETIVA_ERRO={exc!r}", flush=True)
    return result


scraper.login = login_with_collective_capture

if __name__ == "__main__":
    runpy.run_module("scraper_sync_incremental", run_name="__main__")
