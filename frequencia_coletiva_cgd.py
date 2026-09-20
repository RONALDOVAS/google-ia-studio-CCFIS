"""Captura coletiva da tela CGD de frequencias a registrar.

A captura deve reutilizar a pagina ja autenticada pelo sincronizador principal.
Nao abre outro navegador, nao executa novo login e nao cria uma segunda sessao.
"""
import json
from datetime import datetime, timezone
from pathlib import Path

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


def _candidate_routes(page):
    candidates = []
    for text, href in scraper.links(page):
        hay = norm(f"{text} {href}").lower()
        if "frequenc" in hay and any(token in hay for token in ("registr", "pend", "lanç", "lanc")):
            candidates.append(href)
    for text, href in scraper.links(page):
        hay = norm(f"{text} {href}").lower()
        if "frequenc" in hay:
            candidates.append(href)
    return list(dict.fromkeys(candidates))


def capture_on_authenticated_page(page, unidade):
    """Captura a rota coletiva usando a sessao autenticada existente.

    Retorna um snapshot estruturado e restaura a pagina para a URL original
    quando possivel. Nenhum login ou novo contexto de navegador e criado aqui.
    """
    original_url = page.url
    last_error = None

    for href in _candidate_routes(page):
        try:
            if not scraper.open_page(page, href, unidade, "frequencias_coletivas", 800):
                continue
            item = snapshot(page, unidade)
            if item["url"] and (item["texto"] or item["tabelas"]):
                return item
        except Exception as exc:
            last_error = repr(exc)

    if original_url:
        try:
            scraper.open_page(page, original_url, unidade, "retorno_pos_frequencias_coletivas", 300)
        except Exception:
            pass

    return {
        "source": "CGD",
        "unidade": unidade,
        "url": None,
        "capturado_em": datetime.now(timezone.utc).isoformat(),
        "texto": "",
        "tabelas": [],
        "erro": "ROTA_FREQUENCIAS_A_REGISTRAR_NAO_ENCONTRADA",
        "detalhe": last_error,
    }


def capture_and_persist(page, unidade, output=OUTPUT):
    item = capture_on_authenticated_page(page, unidade)
    existing = {"source": "CGD", "gerado_em": datetime.now(timezone.utc).isoformat(), "unidades": []}
    if output.exists():
        try:
            existing = json.loads(output.read_text(encoding="utf-8"))
        except Exception:
            pass

    unidades = [x for x in existing.get("unidades", []) if x.get("unidade") != unidade]
    unidades.append(item)
    existing["unidades"] = unidades
    output.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"[{unidade}] FREQUENCIA_COLETIVA_URL={item.get('url')} "
        f"TABELAS={len(item.get('tabelas') or [])}",
        flush=True,
    )
    return item


if __name__ == "__main__":
    raise SystemExit(
        "Este modulo deve ser chamado pelo sincronizador principal com uma pagina autenticada."
    )
