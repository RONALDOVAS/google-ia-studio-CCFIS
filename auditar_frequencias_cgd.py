"""Auditoria real de frequencia CGD.

A pagina de frequencia do CGD usa a coluna "Obs" para o estado da aula
(Presente/Faltou/Reposicao). Reposicoes nao sao presenca nem falta e, portanto,
ficam fora da equacao de frequencia registrada.

Regra auditada: registros de frequencia = presencas + faltas.
Tambem preserva as capturas de Dashboard, Pacotes de cursos e Frequencias a
Registrar que eram produzidas pela auditoria anterior. Nenhuma regra de negocio
do IA Studio para abatimento de falta por reposicao e aplicada aqui.
"""
import json
import re
from datetime import datetime, date
from pathlib import Path

from playwright.sync_api import sync_playwright

import scraper

ALUNOS = Path("dados_alunos.json")
DASHBOARD = Path("dados_dashboard_cgd.json")
PACOTES = Path("dados_pacotes_cursos.json")
PENDING = Path("dados_frequencias_a_registrar.json")


def norm(v):
    return " ".join(str(v or "").replace("\xa0", " ").split())


def low(v):
    return norm(v).lower()


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
    return low(r.get("status") or r.get("classificacao") or r.get("observacao") or r.get("obs"))


def is_present(status):
    return any(x in status for x in ("presente", "presença", "presenca", "compareceu"))


def is_absent(status):
    return any(x in status for x in ("faltou", "falta", "ausente", "não compareceu", "nao compareceu"))


def recalc_frequency(records):
    registered = []
    pres = 0
    faltas = 0
    reposicoes = 0
    unknown = []
    for r in records or []:
        if not isinstance(r, dict):
            continue
        s = status_of(r)
        if is_present(s):
            pres += 1
            registered.append(r)
        elif is_absent(s):
            faltas += 1
            registered.append(r)
        elif "reposi" in s:
            reposicoes += 1
        elif s:
            unknown.append({"status": r.get("status"), "valores": r.get("valores")})

    dates = [parse_date(r.get("data")) for r in registered if parse_date(r.get("data"))]
    present_dates = [
        parse_date(r.get("data"))
        for r in registered
        if parse_date(r.get("data")) and is_present(status_of(r))
    ]
    return {
        "registros_registrados": len(registered),
        "presencas": pres,
        "faltas": faltas,
        "reposicoes": reposicoes,
        "registros_status_desconhecido": len(unknown),
        "ultimo_acesso": max(present_dates) if present_dates else (max(dates) if dates else None),
        "matematica_ok": len(registered) == pres + faltas and not unknown,
    }


def snapshot_route(page, unidade, path, label):
    url = scraper.CGD_URL.rstrip("/") + path
    if not scraper.open_page(page, url, unidade, label, 500):
        return {"unidade": unidade, "url": url, "ok": False, "texto": "", "tabelas": []}
    return {
        "unidade": unidade,
        "url": page.url,
        "ok": True,
        "texto": scraper.body(page)[:50000],
        "tabelas": [{"cabecalhos": h, "linhas": r} for h, r in scraper.table_data(page)],
        "capturado_em": datetime.utcnow().isoformat() + "Z",
    }


def find_pending_route(page, unidade):
    candidates = []
    for text, href in scraper.links(page):
        hay = low(f"{text} {href}")
        if "frequenc" in hay and any(x in hay for x in ("registr", "pend", "lanç", "lanc")):
            candidates.append(href)
    if not candidates:
        for text, href in scraper.links(page):
            if "frequenc" in low(f"{text} {href}"):
                candidates.append(href)
    for href in dict.fromkeys(candidates):
        if scraper.open_page(page, href, unidade, "frequencias_a_registrar", 800):
            return snapshot_current(page, unidade)
    return {"unidade": unidade, "url": None, "ok": False, "texto": "", "tabelas": []}


def snapshot_current(page, unidade):
    return {
        "unidade": unidade,
        "url": page.url,
        "ok": True,
        "texto": scraper.body(page)[:50000],
        "tabelas": [{"cabecalhos": h, "linhas": r} for h, r in scraper.table_data(page)],
        "capturado_em": datetime.utcnow().isoformat() + "Z",
    }


def main():
    alunos = json.loads(ALUNOS.read_text(encoding="utf-8"))
    if not isinstance(alunos, list) or not alunos:
        raise SystemExit("dados_alunos.json vazio/invalido")

    snapshots_pending = []
    snapshots_dashboard = []
    snapshots_packages = []
    total_checked = 0
    total_bad = 0
    exemplos = []

    with sync_playwright() as pw:
        for unidade in ("matriz", "filial"):
            cfg = scraper.CONFIG[unidade]
            browser = pw.chromium.launch(channel="msedge", headless=False)
            context = browser.new_context()
            page = context.new_page()
            try:
                scraper.login(page, cfg["usuario"], cfg["senha"], unidade)
                snapshots_dashboard.append(snapshot_route(page, unidade, "/dashboard", "dashboard_referencia"))
                snapshots_packages.append(snapshot_route(page, unidade, "/pacotes-cursos", "pacotes_cursos"))
                snapshots_packages.append(snapshot_route(page, unidade, "/relatorios/individuais/matriculados-por-pacotes", "relatorio_matriculados_por_pacote"))
                snapshots_pending.append(find_pending_route(page, unidade))

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
                    aluno["frequencia_reposicoes_cgd"] = calc["reposicoes"]
                    aluno["frequencia_validacao_interna"] = calc
                    aluno["frequencia_status"] = "COM_FREQUENCIA_REAL" if calc["registros_registrados"] else "SEM_FREQUENCIA_A_INVESTIGAR"

                    if len(exemplos) < 5 or cid == "387127":
                        exemplos.append({"unidade": unidade, "contrato": cid, **calc})
                    print(
                        f"[{unidade}] FREQUENCIA_OK cid={cid} registros={calc['registros_registrados']} "
                        f"presencas={calc['presencas']} faltas={calc['faltas']} reposicoes_cgd={calc['reposicoes']} "
                        f"ultimo_acesso={calc['ultimo_acesso']}",
                        flush=True,
                    )
            finally:
                context.close()
                browser.close()

    DASHBOARD.write_text(json.dumps({"source": "CGD", "unidades": snapshots_dashboard}, ensure_ascii=False, indent=2), encoding="utf-8")
    PACOTES.write_text(json.dumps({"source": "CGD", "unidades": snapshots_packages}, ensure_ascii=False, indent=2), encoding="utf-8")
    PENDING.write_text(json.dumps({"source": "CGD", "unidades": snapshots_pending}, ensure_ascii=False, indent=2), encoding="utf-8")
    ALUNOS.write_text(json.dumps(alunos, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"FREQUENCIAS AUDITADAS: {total_checked}", flush=True)
    print(f"FREQUENCIAS COM ERRO MATEMATICO: {total_bad}", flush=True)
    print(f"DASHBOARD CAPTURADO: {sum(bool(x.get('ok')) for x in snapshots_dashboard)}/{len(snapshots_dashboard)}", flush=True)
    print(f"FONTES PACOTES CAPTURADAS: {sum(bool(x.get('ok')) for x in snapshots_packages)}/{len(snapshots_packages)}", flush=True)
    print(f"ROTAS FREQUENCIAS A REGISTRAR CAPTURADAS: {sum(bool(x.get('ok')) for x in snapshots_pending)}/{len(snapshots_pending)}", flush=True)
    print("EXEMPLOS_FREQUENCIA=" + json.dumps(exemplos, ensure_ascii=False), flush=True)

    if total_bad:
        raise SystemExit("Falha: matematica de frequencia inconsistente.")


if __name__ == "__main__":
    main()
