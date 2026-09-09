"""Sincronizacao completa do universo real do CGD (Matriz + Filial).

Nao existe meta fixa de 20, 750 ou qualquer outro numero. A listagem do CGD
define o universo da execucao e cada contrato descoberto e detalhado uma vez.
Os registros antigos sao preservados como fallback quando um campo novo vier
vazio, mas o detalhe capturado passa a ser a fonte atualizada.
"""

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import scraper
import scraper_runner_threaded as runner
from playwright.sync_api import sync_playwright

JSON_PATH = Path("dados_alunos.json")
POPULATION_PATH = Path("dados_populacao_cgd.json")


def _agora_iso():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _contrato(aluno):
    return str(aluno.get("contrato") or aluno.get("cgd_matricula_id") or "").strip()


def _load_existing():
    if not JSON_PATH.exists():
        return []
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("dados_alunos.json nao possui raiz lista")
    print(f"[BASE] REGISTROS_EXISTENTES={len(data)}", flush=True)
    return data


def _merge(existing, capturados):
    antigos = {_contrato(a): a for a in existing if _contrato(a)}
    ordem = [_contrato(a) for a in existing if _contrato(a)]
    for aluno in capturados:
        cid = _contrato(aluno)
        if not cid:
            continue
        anterior = antigos.get(cid, {})
        combinado = dict(anterior)
        for chave, valor in aluno.items():
            if valor is None or valor == "":
                if chave in combinado and combinado[chave] not in (None, ""):
                    continue
            combinado[chave] = valor
        antigos[cid] = combinado
        if cid not in ordem:
            ordem.append(cid)
    return [antigos[cid] for cid in ordem]


def _barra(unidade, etapa, atual, total, inicio, extra=""):
    total = max(1, int(total))
    atual = min(max(0, int(atual)), total)
    pct = atual / total
    largura = 28
    cheios = int(pct * largura)
    vazios = largura - cheios
    decorrido = max(0.001, time.monotonic() - inicio)
    velocidade = atual / decorrido
    restante = max(0, total - atual)
    eta = (restante / velocidade) if velocidade > 0 else 0
    eta_txt = f"{eta/60:.1f}m" if eta >= 60 else f"{eta:.0f}s"
    linha = f"[{unidade.upper()}] {etapa:<12} [{'#' * cheios}{'-' * vazios}] {pct:6.2%} {atual}/{total} ETA {eta_txt}"
    if extra:
        linha += f" | {extra}"
    print("\r[PROGRESSO] " + linha, end="", flush=True)
    if atual == total:
        print("", flush=True)


def _preparar_contexto(context):
    """Reduz trafego visual sem bloquear HTML, JS, XHR ou recursos funcionais."""
    def _route(route):
        tipo = route.request.resource_type
        if tipo in {"image", "font", "media"}:
            route.abort()
        else:
            route.continue_()
    context.route("**/*", _route)


def _discover_unlimited(page, unidade, destino):
    """Descobre todo o universo paginado do CGD sem MAX_CONTRACTS/MAX_PAGES.

    A paginacao termina somente quando o proprio CGD deixa de entregar novos
    contratos. Assim, 20/750/10000 e limites de paginas deixam de participar
    da definicao do universo.
    """
    sr = runner.scraper_runner
    source = sr._listing_source(page, destino)
    print(f"[{unidade}] FONTE_LISTAGEM_FIXA: {source}", flush=True)
    if not scraper.open_page(page, source, unidade, "lista_pagina_1", 300):
        raise RuntimeError(f"[{unidade}] FALHA_ABRINDO_LISTAGEM: {source} final={page.url}")

    first_ids = sr._extract_contract_ids(page.content())
    if not first_ids:
        raise RuntimeError(f"[{unidade}] LISTAGEM_PAGINA_1_SEM_CONTRATOS: {page.url}")

    session = sr._session_from_page(page)
    cookies = {c.name: c.value for c in session.cookies}
    headers = dict(session.headers)
    found = {cid: scraper.contract_url(cid) for cid in first_ids}
    print(f"[{unidade}] LISTAGEM REAL: pagina=1 contratos_p1={len(first_ids)}", flush=True)

    page_number = 2
    empty_streak = 0
    while True:
        url = sr._page_url(source, page_number)
        try:
            _, _, ids, body_size = sr._fetch_listing((unidade, url, cookies, headers))
            before = len(found)
            for cid in ids:
                found[cid] = scraper.contract_url(cid)
            novos = len(found) - before
            print(
                f"[{unidade}] pagina_lista={page_number} contratos_acumulados={len(found)} "
                f"novos={novos} bytes={body_size}",
                flush=True,
            )
            if novos == 0:
                empty_streak += 1
            else:
                empty_streak = 0
            # O fim da paginacao e determinado pelo proprio CGD. Duas paginas
            # sem contrato novo protegem contra uma pagina vazia intermediaria.
            if empty_streak >= 2:
                break
        except Exception as exc:
            raise RuntimeError(f"[{unidade}] FALHA_LISTAGEM_PAGINA={page_number}: {exc}") from exc
        page_number += 1

    print(
        f"[{unidade}] UNIVERSO_DISCOVER_FINAL paginas_visitadas={page_number} "
        f"contratos={len(found)}",
        flush=True,
    )
    if not found:
        raise RuntimeError(f"[{unidade}] UNIVERSO_CGD_VAZIO")
    return list(found.values())


# O sincronizador completo usa explicitamente a descoberta dinamica acima.
# Isso neutraliza os limites historicos existentes no runner legado.
scraper.discover_contracts = _discover_unlimited


def _run_unit(unidade, cfg, pw, existing):
    headless = os.getenv("CGD_HEADLESS", "1").lower() in ("1", "true", "yes", "sim")
    timeout_s = max(15, int(os.getenv("CGD_DETAIL_TIMEOUT_S", "45")))
    inicio = time.monotonic()
    print("=" * 88, flush=True)
    print(f"[{unidade.upper()}] INICIO — UNIVERSO COMPLETO DO CGD", flush=True)
    print(f"[{unidade.upper()}] Navegador: Chromium Playwright (sem Edge)", flush=True)

    browser = pw.chromium.launch(
        headless=headless,
        args=["--disable-gpu", "--disable-dev-shm-usage", "--no-first-run", "--no-default-browser-check"],
    )
    context = browser.new_context()
    context.set_default_timeout(timeout_s * 1000)
    _preparar_contexto(context)
    page = context.new_page()
    try:
        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
        runner._validar_sessao(page, unidade)

        contracts = scraper.discover_contracts(page, unidade, cfg["destino"])
        contratos = []
        ids = set()
        for contract in contracts:
            cid = scraper.contract_id(contract)
            if cid and cid not in ids:
                ids.add(cid)
                contratos.append(contract)
        total = len(contratos)
        if total == 0:
            raise RuntimeError(f"[{unidade}] CGD retornou universo vazio; publicacao sera abortada.")

        print(f"[{unidade.upper()}] UNIVERSO_REAL_DESCoberto={total}", flush=True)
        print(f"[{unidade.upper()}] INICIANDO_DETALHAMENTO=100% DOS CONTRATOS", flush=True)

        reps = scraper.get_replacements(page, unidade)
        print(f"[{unidade.upper()}] REPOSICOES_GLOBAIS_CAPTURADAS={len(reps)}", flush=True)
        runner._validar_sessao(page, unidade)

        capturados = []
        falhas = []
        inicio_detalhes = time.monotonic()
        for index, contract in enumerate(contratos, 1):
            cid = scraper.contract_id(contract)
            try:
                aluno = runner._detalhar_um(page, unidade, contract, reps, index, total)
                if not aluno:
                    raise RuntimeError("detalhe retornou vazio")
                aluno["ultima_sincronizacao_cgd"] = _agora_iso()
                existente = next((a for a in existing if _contrato(a) == cid), None)
                if existente:
                    aluno["primeira_captura_cgd"] = existente.get("primeira_captura_cgd") or existente.get("ultima_sincronizacao_cgd") or aluno["ultima_sincronizacao_cgd"]
                else:
                    aluno.setdefault("primeira_captura_cgd", aluno["ultima_sincronizacao_cgd"])
                capturados.append(aluno)
                _barra(unidade, "DETALHANDO", index, total, inicio_detalhes, f"OK={len(capturados)} ERROS={len(falhas)}")
            except Exception as exc:
                falhas.append({"contrato": cid, "erro": repr(exc)})
                _barra(unidade, "DETALHANDO", index, total, inicio_detalhes, f"OK={len(capturados)} ERROS={len(falhas)}")
                print(f"[{unidade.upper()}] DETALHE_ERRO cid={cid}: {exc!r}", flush=True)
                if runner._pagina_bloqueada(page):
                    raise RuntimeError(f"[{unidade}] BLOQUEIO_CGD durante detalhe cid={cid}") from exc

        print(f"[{unidade.upper()}] DETALHAMENTO_FINALIZADO={len(capturados)}/{total}", flush=True)
        if falhas:
            raise RuntimeError(f"[{unidade}] {len(falhas)} contrato(s) nao foram detalhados: {falhas[:10]}")

        if len(capturados) != total:
            raise RuntimeError(f"[{unidade}] INCOMPLETO: capturados={len(capturados)} universo={total}")

        return capturados, {
            "unidade": unidade,
            "contratos_descobertos": total,
            "alvo_configurado": total,
            "alvo_efetivo": total,
            "persistidos_antes": sum(1 for a in existing if a.get("unidade") == unidade and _contrato(a)),
            "capturados": len(capturados),
            "falhas": len(falhas),
            "estrategia": "UNIVERSO_COMPLETO",
            "navegador": "chromium",
            "duracao_segundos": round(time.monotonic() - inicio, 2),
        }
    finally:
        context.close()
        browser.close()


def main():
    print("=" * 88, flush=True)
    print("SCRAPER CGD — SINCRONIZACAO DO UNIVERSO INTEIRO MATRIZ + FILIAL", flush=True)
    print("SEM LIMITE 20 | SEM LIMITE 750 | SEM REFRESH ROTATIVO PARCIAL", flush=True)
    print("A populacao real descoberta no CGD e o unico universo valido da execucao.", flush=True)
    print("=" * 88, flush=True)

    existing = _load_existing()
    capturados_total = []
    populacoes = []
    erros = []

    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            try:
                capturados, meta = _run_unit(unidade, scraper.CONFIG[unidade], pw, existing)
                capturados_total.extend(capturados)
                populacoes.append(meta)
                print(f"[{unidade.upper()}] UNIDADE_OK {len(capturados)}/{meta['contratos_descobertos']}", flush=True)
            except Exception as exc:
                erros.append({"unidade": unidade, "erro": repr(exc)})
                print(f"[{unidade.upper()}] ERRO_FATAL: {exc!r}", flush=True)

    if erros:
        POPULATION_PATH.write_text(
            json.dumps({"gerado_em": _agora_iso(), "unidades": populacoes, "erros": erros}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        raise RuntimeError(f"Sincronizacao incompleta: {erros}")

    merged = _merge(existing, capturados_total)
    JSON_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    POPULATION_PATH.write_text(
        json.dumps({"gerado_em": _agora_iso(), "unidades": populacoes, "erros": []}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    matriz = sum(1 for a in merged if a.get("unidade") == "matriz")
    filial = sum(1 for a in merged if a.get("unidade") == "filial")
    print("=" * 88, flush=True)
    print(f"[PUBLICACAO] TOTAL_PERSISTIDO={len(merged)} MATRIZ={matriz} FILIAL={filial}", flush=True)
    print(f"[PUBLICACAO] UNIVERSO_CAPTURADO={len(capturados_total)}", flush=True)
    print("[PUBLICACAO] STATUS=PRONTO_PARA_VALIDACAO", flush=True)
    print("=" * 88, flush=True)


if __name__ == "__main__":
    main()
