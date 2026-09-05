"""Gera dados_reposicoes.json usando os dados reais capturados nos alunos.

A fonte principal continua sendo aluno.reposicoes. Como o CGD tambem registra
reposicoes dentro da tabela de frequencia individual, essas linhas sao usadas
como segunda fonte para evitar perder reposicoes quando a tabela global nao
consegue fazer o vinculo pelo contrato/nome.
"""

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

IN = Path("dados_alunos.json")
OUT = Path("dados_reposicoes.json")


def norm(value):
    return " ".join(str(value or "").replace("\xa0", " ").split())


def low(value):
    return norm(value).lower()


def parse_date(value):
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", value)
    if not m:
        return ""
    year = int(m.group(3))
    if year < 100:
        year += 2000
    return f"{int(m.group(1)):02d}/{int(m.group(2)):02d}/{year:04d}"


def parse_times(value):
    return re.findall(r"\b(?:[01]?\d|2[0-3]):[0-5]\d\b", value)


def is_replacement_raw(raw):
    cells = [norm(x) for x in raw.get("valores", [])]
    joined = low(" | ".join(cells))
    return any(token in joined for token in ("reposição", "reposicao", "reposição-faltou", "reposicao-faltou"))


def collect_replacements(aluno):
    out = []
    seen = set()

    for raw in aluno.get("reposicoes") or []:
        if isinstance(raw, dict):
            key = json.dumps(raw, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(raw)

    for raw in aluno.get("frequencia_raw") or []:
        if isinstance(raw, dict) and is_replacement_raw(raw):
            key = json.dumps(raw, ensure_ascii=False, sort_keys=True)
            if key not in seen:
                seen.add(key)
                out.append(raw)

    return out


def transform(aluno, raw, index):
    heads = raw.get("cabecalhos") or []
    cells = [norm(x) for x in raw.get("valores", [])]
    joined = " | ".join(cells)
    date = parse_date(joined)
    times = parse_times(joined)
    start = times[0] if times else None
    end = times[1] if len(times) > 1 else None
    discipline = "Módulo Geral"
    for cell in cells:
        low_cell = low(cell)
        if any(k in low_cell for k in ("informática", "informatica", "módulo", "modulo", "disciplina", "excel", "word", "powerpoint")):
            discipline = cell
            break
    key = "|".join([
        str(aluno.get("unidade") or ""),
        str(aluno.get("contrato") or ""),
        str(aluno.get("nome") or ""),
        date,
        start or "",
        discipline,
        str(index),
    ]).lower()
    rid = "cgd_rep_" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
    return {
        "id": rid,
        "aluno_id": aluno.get("cgd_matricula_id"),
        "aluno_nome": aluno.get("nome"),
        "contrato": aluno.get("contrato"),
        "unidade": aluno.get("unidade"),
        "data": date or None,
        "horario_inicio": start,
        "horario_fim": end,
        "duracao_horas": 2,
        "disciplina": discipline,
        "professor": aluno.get("professor") or "Ronaldo Vasconcelos",
        "status": "agendada",
        "tipo": "laboratorio",
        "observacao": "Capturado dentro do detalhe real do CGD",
        "cabecalhos": heads,
        "valores": cells,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }


def main():
    if not IN.exists():
        raise SystemExit("dados_alunos.json não encontrado")
    alunos = json.loads(IN.read_text(encoding="utf-8"))
    out = {}
    total_fontes = 0
    for aluno in alunos:
        raws = collect_replacements(aluno)
        total_fontes += len(raws)
        for index, raw in enumerate(raws):
            record = transform(aluno, raw, index)
            out[record["id"]] = record
    payload = {
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "source": "CGD",
        "records": list(out.values()),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"FONTES DE REPOSICAO ENCONTRADAS: {total_fontes}")
    print(f"REPOSICOES A PARTIR DOS ALUNOS: {len(payload['records'])}")


if __name__ == "__main__":
    main()
