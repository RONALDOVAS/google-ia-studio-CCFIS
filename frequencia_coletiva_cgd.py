"""Captura coletiva da tela CGD de frequencias a registrar.

Este modulo nao inventa seletores de sala/data/horario. Ele navega pela rota
real descoberta no menu autenticado, registra a URL efetivamente encontrada,
e extrai tabelas/texto renderizados para posterior cruzamento com o universo.
A captura e independente do detalhamento individual de cada contrato.
"""
import json
import os
from datetime import datetime, timezone
from pathlib import Path

from playwright.sync_api import sync_playwright

import scraper

OUTPUT = Path("dados_frequencias_a_registrar.json")


def norm(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def snapshot(page, unidade):
    return {
        "source": "CGD",
        "unidade": unidade,
        "url": page.url,
        "capturado_em": datetime.now(timezone.utc).isoformat(),
        "texto": scraper.body(page)[:100000],
        "tabelas": [
            {"cabecalhos": heads, "linhas": rows}
            for heads, rows in scraper.table_data(page)
        ],
    }


def find_route(page, unidade):
    candidates = []
    for text, href in scraper.links(page):
        hay = norm(f"{text} {href}").lower()
        if "frequenc" in hay and any(token in hay for token in ("registr", "pend", "lanç", "lanc")):
            candidates.append(href)
    for text, href in scraper.links(page):
        hay = norm(f"{text} {href}").lower()
        if "frequenc" in hay:
            candidates.append(href)

    for href in dict.fromkeys(candidates):
        if scraper.open_page(page, href, unidade, "frequencias_coletivas", 800):
            return snapshot(page, unidade)

    return {
        "source": "CGD",
        "unidade": unidade,
        "url": None,
        "capturado_em": datetime.now(timezone.utc).isoformat(),
        "texto": "",
        "tabelas": [],
        "erro": "ROTA_FREQUENCIAS_A_REGISTRAR_NAO_ENCONTRADA",
    }


def main():
    resultado = {"source": "CGD", "gerado_em": datetime.now(timezone.utc).isoformat(), "unidades": []}
    headless = os.getenv("CGD_HEADLESS", "false").lower() in ("1", "true", "yes", "sim")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=headless)
        try:
            for unidade in ("matriz", "filial"):
                cfg = scraper.CONFIG[unidade]
                context = browser.new_context()
                page = context.new_page()
                try:
                    scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
                    item = find_route(page, unidade)
                    resultado["unidades"].append(item)
                    print(
                        f"[{unidade}] FREQUENCIA_COLETIVA_URL={item.get('url')} "
                        f"TABELAS={len(item.get('tabelas') or [])}",
                        flush=True,
                    )
                finally:
                    context.close()
        finally:
            browser.close()

    OUTPUT.write_text(json.dumps(resultado, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(bool(item.get("url")) for item in resultado["unidades"])
    print(f"ROTAS_COLETIVAS_CAPTURADAS={ok}/{len(resultado['unidades'])}", flush=True)

    if ok == 0:
        raise SystemExit("Nenhuma rota coletiva de frequencias foi encontrada.")


if __name__ == "__main__":
    main()
