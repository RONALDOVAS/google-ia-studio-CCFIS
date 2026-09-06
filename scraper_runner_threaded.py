"""Executor CGD: filtra campanha antes do detalhe e usa o detalhamento paralelo original."""

import json
import os
from pathlib import Path

import scraper
import scraper_runner
from playwright.sync_api import sync_playwright

JSON_PATH = Path("dados_alunos.json")
CAMPAIGN = "informatica"


def _norm(value):
    return scraper_runner._norm_search(value)


def _load_existing():
    if not JSON_PATH.exists():
        return []
    data = json.loads(JSON_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise RuntimeError("dados_alunos.json não possui raiz lista")
    print(f"[INCREMENTAL] DADOS_EXISTENTES={len(data)}", flush=True)
    return data


def _contract_key(aluno):
    return str(aluno.get("contrato") or aluno.get("cgd_matricula_id") or "").strip()


def _merge(existing, novos):
    merged = {}
    for aluno in existing + novos:
        cid = _contract_key(aluno)
        if cid:
            merged[cid] = aluno
    return list(merged.values())


def _unit_existing(existing, unidade):
    return {_contract_key(a) for a in existing if a.get("unidade") == unidade and _contract_key(a)}


def _save_incremental(existing, novos):
    merged = _merge(existing, novos)
    JSON_PATH.write_text(json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8")
    return merged


def _run_unit(unidade, cfg, pw, existing):
    existing_ids = _unit_existing(existing, unidade)
    alvo = max(1, int(os.getenv("CGD_DETAIL_TARGET_PER_UNIT", "50")))
    print(f"\n[{unidade}] ================================================================", flush=True)
    print(f"[{unidade}] FILTRO CAMPANHA ATIVO: somente campanha contendo '{CAMPAIGN}'", flush=True)
    print(f"[{unidade}] ALVO CUMULATIVO: {alvo} | EXISTENTES NA UNIDADE: {len(existing_ids)}", flush=True)

    browser = pw.chromium.launch(channel="msedge", headless=os.getenv("CGD_HEADLESS", "false").lower() in ("1", "true", "yes", "sim"))
    context = browser.new_context()
    page = context.new_page()
    state_path = Path("edge_cgd_profiles") / unidade / "storage_state_incremental.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
        contracts = scraper_runner.optimized_discover_contracts(page, unidade, cfg["destino"])
        reps = scraper.get_replacements(page, unidade)
        context.storage_state(path=str(state_path))
    finally:
        context.close()
        browser.close()

    # optimized_discover_contracts já faz o filtro de campanha por contrato
    # antes do detalhamento. Aqui removemos apenas o que já está persistido.
    novos_urls = []
    for contract in contracts:
        cid = scraper.contract_id(contract)
        if cid and cid not in existing_ids:
            novos_urls.append(contract)

    limite = max(0, alvo - len(existing_ids))
    novos_urls = novos_urls[:limite]
    print(f"[{unidade}] CONTRATOS INFORMATICA ENCONTRADOS={len(contracts)}", flush=True)
    print(f"[{unidade}] CONTRATOS NOVOS INFORMATICA={len(novos_urls)}", flush=True)
    print(f"[{unidade}] INICIANDO DETALHAMENTO PARALELO: workers={os.getenv('CGD_DETAIL_WORKERS', '3')}", flush=True)

    novos = scraper.process_details(unidade, cfg, novos_urls, reps, str(state_path)) if novos_urls else []

    validos = []
    rejeitados = 0
    for aluno in novos:
        campanha = _norm(aluno.get("campanha"))
        if CAMPAIGN not in campanha:
            rejeitados += 1
            print(f"[{unidade}] REJEITADO_POS_DETALHE cid={_contract_key(aluno)} campanha={aluno.get('campanha')!r}", flush=True)
            continue
        if not (aluno.get("frequencia_raw") or []):
            rejeitados += 1
            print(f"[{unidade}] REJEITADO_SEM_FREQUENCIA cid={_contract_key(aluno)}", flush=True)
            continue
        validos.append(aluno)
        print(f"[{unidade}] ALUNO_VALIDADO campanha={aluno.get('campanha')!r} cid={_contract_key(aluno)} nome={aluno.get('nome')} faltas={aluno.get('faltas')}", flush=True)

    print(f"[{unidade}] DETALHAMENTO_FINAL: validos={len(validos)} rejeitados={rejeitados}", flush=True)
    return validos


def main_incremental():
    print("=" * 80, flush=True)
    print("SCRAPER CGD - INFORMATICA SOMENTE + DETALHAMENTO PARALELO", flush=True)
    print("Fluxo: login -> filtrar campanha na listagem -> ignorar persistidos -> detalhe paralelo -> validar -> salvar", flush=True)
    print("=" * 80, flush=True)

    existing = _load_existing()
    novos_total = []
    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            novos = _run_unit(unidade, scraper.CONFIG[unidade], pw, existing)
            novos_total.extend(novos)
            existing = _save_incremental(existing, novos)
            print(f"[INCREMENTAL] SALVO_APOS_{unidade.upper()} total={len(existing)}", flush=True)

    por_unidade = {u: sum(1 for a in existing if a.get("unidade") == u) for u in ("matriz", "filial")}
    campanhas = {}
    for aluno in existing:
        c = str(aluno.get("campanha") or "").strip() or "SEM_CAMPANHA"
        campanhas[c] = campanhas.get(c, 0) + 1

    print("=" * 80, flush=True)
    print(f"[INCREMENTAL] NOVOS_VALIDOS={len(novos_total)}", flush=True)
    print(f"[INCREMENTAL] TOTAL_PERSISTIDO={len(existing)} matriz={por_unidade['matriz']} filial={por_unidade['filial']}", flush=True)
    print(f"[INCREMENTAL] DISTRIBUICAO_CAMPANHAS={campanhas}", flush=True)
    print("=" * 80, flush=True)


scraper.discover_contracts = scraper_runner.optimized_discover_contracts

if __name__ == "__main__":
    main_incremental()
