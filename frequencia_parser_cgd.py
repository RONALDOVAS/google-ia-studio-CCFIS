"""Parser da frequencia individual real do CGD.

O CGD renderiza a tabela de frequencia individual via JavaScript. Em alguns
momentos o DOM/HTML nao expoe uma <table> utilizavel, mas o texto renderizado
contém a estrutura: Data ... Obs ... Lição ... Excluir.

Este parser trabalha sobre o texto renderizado e usa a coluna/rotulo Obs como
fonte autoritativa do estado da aula. Reposicao e Reposicao-Faltou ficam
separadas de presenca/falta; isso evita transformar uma reposicao em uma falta
academica comum.
"""

import re
from datetime import date

STATUS_RE = re.compile(r"\b(Reposição-Faltou|Reposicao-Faltou|Reposição|Reposicao|Faltou|Presente)\b", re.I)
DATE_RE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")


def norm(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def parse_date(value):
    m = DATE_RE.search(str(value or ""))
    if not m:
        return None
    d, mth, y = map(int, re.split(r"[/-]", m.group(0)))
    if y < 100:
        y += 2000
    try:
        return date(y, mth, d).isoformat()
    except ValueError:
        return None


def _status_kind(value):
    s = norm(value).lower()
    s = s.replace("ç", "c").replace("ã", "a")
    if "reposicao-faltou" in s:
        return "reposicao_faltou"
    if "reposicao" in s:
        return "reposicao"
    if "faltou" in s or "ausente" in s or "nao compareceu" in s:
        return "falta"
    if "presente" in s or "compareceu" in s:
        return "presenca"
    return None


def parse_frequency_text(text):
    text = norm(text)
    start = text.lower().find("frequência de cursos individuais")
    if start < 0:
        start = text.lower().find("frequencia de cursos individuais")
    if start >= 0:
        text = text[start:]
    end_markers = ("Frequência de turmas", "Frequencia de turmas")
    for marker in end_markers:
        idx = text.lower().find(marker.lower())
        if idx > 0:
            text = text[:idx]
            break

    dates = list(DATE_RE.finditer(text))
    records = []
    for i, dm in enumerate(dates):
        chunk = text[dm.start():dates[i + 1].start() if i + 1 < len(dates) else len(text)]
        sm = STATUS_RE.search(chunk)
        if not sm:
            continue
        status = sm.group(1)
        kind = _status_kind(status)
        if not kind:
            continue
        iso = parse_date(dm.group(0))
        records.append({
            "data": dm.group(0),
            "data_iso": iso,
            "status": status,
            "classificacao": kind,
            "valores": norm(chunk).split(" Excluir", 1)[0].split(" Visualizar", 1)[0],
            "cabecalhos": ["Data", "Horário", "Sala", "Curso", "Aula", "Conteúdo", "Obs", "Lição", "Passo", "Seq"],
        })

    presencas = sum(r["classificacao"] == "presenca" for r in records)
    faltas = sum(r["classificacao"] == "falta" for r in records)
    reposicoes = sum(r["classificacao"] in ("reposicao", "reposicao_faltou") for r in records)
    return {
        "faltas": faltas,
        "presencas": presencas,
        "reposicoes": reposicoes,
        "registros": records,
    }


def parse_frequency_page(page):
    return parse_frequency_text(page.locator("body").inner_text())
