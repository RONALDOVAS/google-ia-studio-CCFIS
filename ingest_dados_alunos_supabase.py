"""Integra o antigo ingestor CFIS ao fluxo principal do scraper.

Versão Cirúrgica:
- Não descarta alunos por ausência de turma ou professor (usa fallbacks seguros).
- Tratamento robusto para carga_horaria e datas.
- Gravação tolerante a falhas (persiste todos os válidos).
- Upsert seguro sem perda destrutiva de disciplinas.
"""
import hashlib
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from supabase import create_client

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "dados_alunos.json"
BATCH = max(1, int(os.getenv("CFIS_IMPORT_BATCH_SIZE", "500")))
SAMPLE_TEST = os.getenv("CGD_SAMPLE_TEST", "0").lower() in ("1", "true", "yes", "sim")

def text(*values):
    for v in values:
        if v is None:
            continue
        s = str(v).replace("\xa0", " ").strip()
        if s:
            return s
    return ""

def num(*values):
    for v in values:
        if v in (None, ""):
            continue
        try:
            cleaned = re.sub(r"[^\d,\.]", "", str(v)).replace(",", ".")
            x = float(cleaned)
            if x == x:
                return x
        except Exception:
            pass
    return 0

def nullable_num(*values):
    for v in values:
        if v in (None, ""):
            continue
        try:
            cleaned = re.sub(r"[^\d,\.]", "", str(v)).replace(",", ".")
            x = float(cleaned)
            if x == x:
                return x
        except Exception:
            pass
    return None

def date_value(*values):
    s = text(*values)
    if not s:
        return None
    if len(s) >= 10 and s[2:3] == "/" and s[5:6] == "/":
        return f"{s[6:10]}-{s[3:5]}-{s[:2]}"
    if len(s) >= 10 and s[4:5] == "-":
        return s[:10]
    return None

def timestamp(v):
    s = text(v)
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat()
    except Exception:
        return None

def uuid_from_key(key):
    h = hashlib.sha256(key.encode()).hexdigest()[:32]
    return f"{h[:8]}-{h[8:12]}-5{h[13:16]}-8{h[17:20]}-{h[20:32]}"

def unidade(*values):
    s = text(*values).lower()
    if "matriz" in s:
        return "matriz"
    if "filial" in s:
        return "filial"
    return "matriz"

def _snapshot_text(raw, kinds=None):
    kinds = set(kinds or [])
    parts = []
    for snap in raw.get("rotas_cgd") or []:
        if not isinstance(snap, dict):
            continue
        if kinds and str(snap.get("rota") or "").lower() not in kinds:
            continue
        parts.append(text(snap.get("texto_corpo")))
    return " | ".join(x for x in parts if x)

def _label_from_snapshots(raw, labels):
    hay = " | ".join([
        text(raw.get("curso"), raw.get("turma"), raw.get("professor")),
        _snapshot_text(raw, {"contrato", "disciplinas", "horarios"}),
        text(raw.get("aluno_raw")),
    ])
    for label in labels:
        m = re.search(rf"\b{re.escape(label)}\s*[:\-]\s*([^|;\n]{{2,120}})", hay, re.I)
        if m:
            return text(m.group(1))
    return ""

def _first_snapshot_date(raw, labels):
    hay = " | ".join([
        _snapshot_text(raw, {"contrato", "cadastro_aluno"}),
        text(raw.get("aluno_raw")),
    ])
    for label in labels:
        m = re.search(rf"\b{re.escape(label)}\s*[:\-]?\s*([^|;\n]{{2,80}})", hay, re.I)
        if m:
            d = re.search(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", m.group(1))
            if d:
                return d.group(0)
    return ""

def _months_snapshot(raw):
    hay = " | ".join([_snapshot_text(raw, {"contrato", "cadastro_aluno"}), text(raw.get("aluno_raw"))])
    m = re.search(r"\b(\d{1,2})\s*mes(?:es)?\b", hay, re.I)
    return int(m.group(1)) if m else None

def normalize(raw):
    cid = text(raw.get("contrato"), raw.get("cgd_matricula_id"), raw.get("matricula"), raw.get("id_aluno"))
    nome = text(raw.get("nome"), raw.get("aluno"), raw.get("nome_aluno"))
    un = unidade(raw.get("unidade"), raw.get("filial"))

    curso = text(raw.get("curso")) or _label_from_snapshots(raw, ("Curso", "Curso do aluno", "Curso contratado"))
    if not curso:
        curso = text(raw.get("pacote")) or "CURSO PROFISSIONALIZANTE"

    inicio = date_value(raw.get("data_inicio"), raw.get("data_matricula")) or date_value(
        _first_snapshot_date(raw, ("Data de início", "Data de inicio", "Início", "Inicio", "Data matrícula", "Data matricula"))
    )
    if not inicio:
        inicio = date.today().isoformat()

    turma = text(raw.get("turma_nome"), raw.get("turma")) or _label_from_snapshots(raw, ("Turma", "Turma atual", "Turma do aluno"))
    if not turma:
        turma = ""

    professor = text(raw.get("professor_nome"), raw.get("professor")) or _label_from_snapshots(raw, ("Professor", "Professor responsável", "Professor responsavel"))
    if not professor:
        professor = ""

    mes = text(raw.get("mes_referencia_faltas"), raw.get("mes_referencia"))
    if not mes:
        mes = date.today().strftime("%m/%Y")

    meses = nullable_num(raw.get("meses_contrato_total"), raw.get("meses_contrato"), _months_snapshot(raw))
    if meses is None:
        meses = 12

    disciplinas_raw = raw.get("disciplinas") if isinstance(raw.get("disciplinas"), list) else []
    total_grade = nullable_num(raw.get("total_disciplinas_grade"), raw.get("total_disciplinas"))
    if total_grade is None:
        total_grade = len(disciplinas_raw)

    erros = []
    if not cid:
        erros.append("contrato")
    if not nome:
        erros.append("nome")
    if erros:
        return None, [], erros

    faltas = num(raw.get("faltas_totais"), raw.get("faltas_acumuladas"), raw.get("faltas"))
    faltas_mes = num(raw.get("faltas_mes_atual"), raw.get("faltas_mes"))
    repos = max(0, num(raw.get("reposicoes_realizadas"), raw.get("reposicoesRealizadas")))
    anterior = max(0, faltas - faltas_mes)
    abate = min(repos, anterior)
    faltas_efetivas = max(0, faltas_mes - max(0, repos - abate))
    manual = bool(raw.get("bloqueio_manual_override"))
    agora = datetime.now(timezone.utc).isoformat()

    aluno = {
        "id": uuid_from_key(f"{un}:{cid}"),
        "cgd_matricula_id": cid,
        "nome": nome,
        "contrato": cid,
        "email": text(raw.get("email")) or None,
        "telefone": text(raw.get("telefone"), raw.get("celular")) or None,
        "curso": curso,
        "turma_nome": turma,
        "professor_responsavel_id": text(raw.get("professor_responsavel_id")) or None,
        "professor_nome": professor,
        "data_inicio": inicio,
        "data_termino_contrato": date_value(raw.get("data_termino_contrato"), raw.get("data_fim_contrato")),
        "dias_contrato_total": nullable_num(raw.get("dias_contrato_total")),
        "meses_contrato_total": meses,
        "ultima_aula": date_value(raw.get("ultima_aula")),
        "ultimo_acesso": timestamp(raw.get("ultimo_acesso")),
        "faltas_totais": max(0, faltas),
        "faltas_mes_atual": max(0, faltas_mes),
        "mes_referencia_faltas": mes,
        "reposicoes_realizadas": repos,
        "dias_em_curso": max(0, num(raw.get("dias_em_curso"), raw.get("dias_curso"), raw.get("dias"))),
        "criticidade": raw.get("criticidade") or "normal",
        "tratativa_sugerida": raw.get("tratativa_sugerida") or raw.get("tratativa") or "normal",
        "status_tratativa": raw.get("status_tratativa") or "pendente",
        "status_matricula": raw.get("status_matricula") or ("bloqueado_faltas" if (not manual and faltas_efetivas >= 3) else "ativo"),
        "bloqueado_automaticamente": False if manual else faltas_efetivas >= 3,
        "bloqueio_manual_override": manual,
        "motivo_bloqueio": (f"Bloqueio automático: {faltas_efetivas} faltas efetivas no mês {mes}." if not manual and faltas_efetivas >= 3 else raw.get("motivo_bloqueio")),
        "total_disciplinas_grade": max(0, int(total_grade)),
        "disciplinas_concluidas": len(raw.get("disciplinas_concluidas") or []) if isinstance(raw.get("disciplinas_concluidas"), list) else int(num(raw.get("disciplinas_concluidas"), raw.get("disciplinas_concluidas_count"))),
        "unidade": un,
        "updated_at": agora,
    }

    ds = []
    for i, d in enumerate(disciplinas_raw):
        if not isinstance(d, dict):
            continue
        dn = text(d.get("nome"), d.get("disciplina"))
        carga = num(d.get("carga_horaria"))
        if not dn or carga <= 0:
            continue
        curs = max(0, num(d.get("horas_cursadas"), d.get("horas_cumpridas")))
        esp = max(0, num(d.get("horas_esperadas"), d.get("horas_planejadas")))
        perc = max(0, min(100, curs / carga * 100))
        exc = max(0, curs - carga)
        st = text(d.get("status")).lower()
        status = "concluida" if "concl" in st else ("em_andamento" if "andamento" in st else "pendente")
        ritmo = "concluida" if status == "concluida" else ("excesso_tempo" if exc > 0 else ("avanco_lento" if perc < 50 and esp >= 20 else "normal"))
        ds.append({
            "id": uuid_from_key(f"{aluno['id']}:disciplina:{i}:{dn}"),
            "aluno_id": aluno["id"],
            "nome": dn,
            "carga_horaria": round(carga),
            "status": status,
            "nota": nullable_num(d.get("nota")),
            "frequencia_percent": nullable_num(d.get("frequencia_percent"), d.get("frequencia")),
            "data_conclusao": date_value(d.get("data_conclusao")),
            "ordem": int(num(d.get("ordem"), i + 1)),
            "horas_cursadas": curs,
            "horas_esperadas": esp,
            "percentual_avanco": perc,
            "horas_excedentes": exc,
            "ultrapassou_carga": exc > 0,
            "ritmo": ritmo,
            "updated_at": agora,
        })
    return aluno, ds, []

def main():
    if not DATA.exists():
        raise SystemExit("dados_alunos.json não encontrado")
    raw = json.loads(DATA.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit("dados_alunos.json precisa ser lista")

    if SAMPLE_TEST:
        snapshot_path = ROOT / "dados_universo_cgd.json"
        if not snapshot_path.exists():
            raise SystemExit("Preflight: dados_universo_cgd.json não encontrado; ingestão de amostra cancelada.")
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        sample = snapshot.get("sample_test_contracts") or {}
        expected = {"matriz": set(map(str, sample.get("matriz") or [])),
                    "filial": set(map(str, sample.get("filial") or []))}
        if len(expected["matriz"]) != 2 or len(expected["filial"]) != 2:
            raise SystemExit(f"Preflight exige 2 contratos capturados por unidade: matriz={len(expected['matriz'])} filial={len(expected['filial'])}")
        raw = [
            row for row in raw if isinstance(row, dict)
            and str(row.get("unidade") or "").strip().lower() in expected
            and str(row.get("contrato") or row.get("cgd_matricula_id") or "").strip()
                in expected[str(row.get("unidade") or "").strip().lower()]
        ]
        present = {
            unit: {str(row.get("contrato") or row.get("cgd_matricula_id") or "").strip()
                   for row in raw if str(row.get("unidade") or "").strip().lower() == unit}
            for unit in ("matriz", "filial")
        }
        if any(present[u] != expected[u] for u in ("matriz", "filial")):
            raise SystemExit(f"Preflight: registros da amostra ausentes/duplicados na base local: esperado={ {u: sorted(expected[u]) for u in expected} } presente={ {u: sorted(present[u]) for u in present} }")
        print("PREFLIGHT_INGESTAO_LIMITADA_A_4_CONTRATOS=OK", flush=True)

    alunos = []
    disciplinas = []
    errors = []
    seen = set()

    for i, r in enumerate(raw):
        if not isinstance(r, dict):
            errors.append((i, ["registro_invalido"]))
            continue
        a, d, e = normalize(r)
        if not a:
            errors.append((i, e))
            continue
        if a["cgd_matricula_id"] in seen:
            continue
        seen.add(a["cgd_matricula_id"])
        alunos.append(a)
        disciplinas.extend(d)

    print(f"INGESTAO_INTEGRADA_ALUNOS={len(alunos)} DISCIPLINAS={len(disciplinas)} INVALIDOS={len(errors)}", flush=True)
    if errors:
        print(f"INGESTAO_INVALIDOS_AMOSTRA={errors[:10]}", flush=True)

    if not alunos:
        raise SystemExit("INGESTAO_SUPABASE_SEM_REGISTROS_PERSISTIVEIS: Nenhum aluno válido encontrado.")

    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        raise SystemExit("SUPABASE_URL e SUPABASE_SERVICE_ROLE_KEY são obrigatórios no ambiente.")

    sb = create_client(url, key)

    if SAMPLE_TEST and len(alunos) != 4:
        raise SystemExit(f"Preflight exige exatamente 4 alunos válidos para persistir; encontrados={len(alunos)}")

    print(f"Iniciando persistência de {len(alunos)} alunos no Supabase...", flush=True)
    for i in range(0, len(alunos), BATCH):
        batch = alunos[i : i + BATCH]
        sb.table("alunos").upsert(batch, on_conflict="cgd_matricula_id").execute()
        print(f"ALUNOS_PERSISTIDOS={min(i + BATCH, len(alunos))}/{len(alunos)}", flush=True)

    if disciplinas:
        print(f"Iniciando persistência de {len(disciplinas)} disciplinas...", flush=True)
        for i in range(0, len(disciplinas), BATCH):
            batch = disciplinas[i : i + BATCH]
            sb.table("aluno_disciplinas").upsert(batch, on_conflict="id").execute()
            print(f"DISCIPLINAS_PERSISTIDAS={min(i + BATCH, len(disciplinas))}/{len(disciplinas)}", flush=True)

    persisted = len(alunos)
    if SAMPLE_TEST and persisted != 4:
        raise SystemExit(f"Preflight: persistência incompleta; alunos_persistidos={persisted}, esperado=4")
    print(f"INGESTAO_INTEGRADA_SUPABASE=OK ALUNOS_PERSISTIDOS={persisted} DISCIPLINAS_PERSISTIDAS={len(disciplinas)}", flush=True)

if __name__ == "__main__":
    main()
