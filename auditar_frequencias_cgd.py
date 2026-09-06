"""Auditoria de frequencia CGD sem confundir registros com linhas de tabela.

Regra: frequencias registradas = presencas + faltas. Linhas auxiliares do CGD
nao entram na contagem. O ultimo_acesso e a maior data registrada como Presente.
Tambem captura a tela/rota de Frequencias a Registrar quando ela estiver
exposta na navegacao da unidade. Reposicoes sao apenas lidas do CGD; nenhuma
regra do IA Studio e aplicada aqui.
"""
import json
import re
from datetime import datetime, date
from pathlib import Path

from playwright.sync_api import sync_playwright

import scraper

ALUNOS = Path("dados_alunos.json")
PENDING = Path("dados_frequencias_a_registrar.json")


def parse_date(value):
    m = re.search(r"\b(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})\b", str(value or ""))
    if not m:
        return None
    d, mth, y = map(int, m.groups())
    if y < 100:
        y += 2000
    try:
        return date(y, mth, d).isoformat()
    except ValueError:
        return None


def status_of(r):
    return scraper.low(r.get("status") or r.get("classificacao"))


def recalc_frequency(records):
    registered = []
    pres = 0
    faltas = 0
    for r in records or []:
        if not isinstance(r, dict):
            continue
        s = status_of(r)
        if any(x in s for x in ("presente", "presença", "presenca", "compareceu")):
            pres += 1
            registered.append(r)
        elif any(x in s for x in ("falta", "ausente", "não compareceu", "nao compareceu")):
            faltas += 1
            registered.append(r)
    dates = [parse_date(r.get("data")) for r in registered if parse_date(r.get("data"))]
    present_dates = [parse_date(r.get("data")) for r in registered if parse_date(r.get("data")) and any(x in status_of(r) for x in ("presente", "presença", "presenca", "compareceu"))]
    return {
        "registros_registrados": len(registered),
        "presencas": pres,
        "faltas": faltas,
        "ultimo_acesso": max(present_dates) if present_dates else (max(dates) if dates else None),
        "matematica_ok": len(registered) == pres + faltas,
    }


def find_pending_route(page, unidade):
    candidates = []
    for text, href in scraper.links(page):
        hay = scraper.low(f"{text} {href}")
        if "frequenc" in hay and any(x in hay for x in ("registr", "pend", "lanç", "lanc")):
            candidates.append(href)
    # fallback: links explicitamente relacionados a frequencias.
    if not candidates:
        for text, href in scraper.links(page):
            if "frequenc" in scraper.low(f"{text} {href}"):
                candidates.append(href)
    for href in dict.fromkeys(candidates):
        if scraper.open_page(page, href, unidade, "frequencias_a_registrar", 800):
            return {
                "unidade": unidade,
                "url": page.url,
                "texto": scraper.body(page)[:50000],
                "tabelas": [{"cabecalhos": h, "linhas": r} for h, r in scraper.table_data(page)],
                "capturado_em": datetime.utcnow().isoformat() + "Z",
            }
    return {"unidade": unidade, "url": None, "texto": "", "tabelas": [], "capturado_em": datetime.utcnow().isoformat() + "Z"}


def main():
    alunos = json.loads(ALUNOS.read_text(encoding="utf-8"))
    if not isinstance(alunos, list) or not alunos:
        raise SystemExit("dados_alunos.json vazio/invalido")
    snapshots = []
    total_checked = 0
    total_bad = 0
    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            cfg = scraper.CONFIG[unidade]
            browser = pw.chromium.launch(channel="msedge", headless=False)
            context = browser.new_context()
            page = context.new_page()
            try:
                scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
                snapshots.append(find_pending_route(page, unidade))
                for aluno in [a for a in alunos if a.get("unidade") == unidade]:
                    cid = str(aluno.get("contrato") or aluno.get("cgd_matricula_id") or "").strip()
                    if not cid:
                        continue
                    url = scraper.child_url(cid, "frequencias")
                    if not scraper.open_page(page, url, unidade, f"frequencia_auditoria_{cid}", 800):
                        raise RuntimeError(f"[{unidade}] FREQUENCIA_NAO_ACESSIVEL cid={cid}")
                    freq = scraper.extract_frequency(page, cid)
                    calc = recalc_frequency(freq.get("registros"))
                    total_checked += 1
                    if not calc["matematica_ok"]:
                        total_bad += 1
                        raise RuntimeError(f"[{unidade}] FREQUENCIA_MATEMATICA_INVALIDA cid={cid}: {calc}")
                    aluno["faltas"] = calc["faltas"]
                    aluno["presencas"] = calc["presencas"]
                    aluno["ultimo_acesso"] = calc["ultimo_acesso"]
                    aluno["frequencia_registrada"] = calc["registros_registrados"]
                    aluno["frequencia_validacao_interna"] = calc
                    aluno["frequencia_status"] = "COM_FREQUENCIA_REAL" if calc["registros_registrados"] else "SEM_FREQUENCIA_A_INVESTIGAR"
                    print(f"[{unidade}] FREQUENCIA_OK cid={cid} registros={calc['registros_registrados']} presencas={calc['presencas']} faltas={calc['faltas']} ultimo_acesso={calc['ultimo_acesso']}", flush=True)
            finally:
                context.close()
                browser.close()
    PENDING.write_text(json.dumps({"source": "CGD", "unidades": snapshots}, ensure_ascii=False, indent=2), encoding="utf-8")
    ALUNOS.write_text(json.dumps(alunos, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"FREQUENCIAS AUDITADAS: {total_checked}", flush=True)
    print(f"FREQUENCIAS COM ERRO MATEMATICO: {total_bad}", flush=True)
    print(f"ROTAS FREQUENCIAS A REGISTRAR CAPTURADAS: {sum(bool(x.get('url')) for x in snapshots)}/{len(snapshots)}", flush=True)
    if total_bad:
        raise SystemExit("Falha: matematica de frequencia inconsistente.")


if __name__ == "__main__":
    main()
