"""Dev-Check — herramienta de autoayuda para desarrolladores CODELAR.

Cada dev la corre con SU PROPIO token de ClickUp. La herramienta auto-detecta su
perfil (GET /user) y analiza SOLO sus tareas del sprint activo, mostrándole qué
debe ajustar frente al lineamiento del equipo.

Sigue el patrón de tdd_report.py: TODO se calcula de forma DETERMINÍSTICA (rápido y
consistente) y la parte AI es OPCIONAL (--analyze): un único bloque acotado
[COACHING] con delimitadores estrictos, para no depender de la lentitud/variabilidad
de la AI. Por defecto NO llama a la AI.

Uso:
  python dev_check.py --setup     # descubre y cachea sprints (config.json)
  python dev_check.py             # mi diagnóstico determinístico (rápido, sin AI)
  python dev_check.py --analyze   # + coaching AI ("qué ajustar hoy")
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE_URL = "https://api.clickup.com/api/v2"
ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
REPORT_NAME = "Mi_Reporte_{date}.md"
DATA_NAME = "mi_data_{date}.json"
ANALYSIS_CONTEXT_NAME = "analysis_context.md"

# ── Configuración de proyectos / custom fields (derivada de tdd_report.py) ─────
# Nota: estos IDs no son secretos (son inútiles sin un token con acceso). El perfil
# del dev se auto-detecta de su token, por eso aquí no hay roster nominal del equipo.
PROJECTS = {
    "GGPx": {"space": "90010099560", "folder": "90175043951"},
    "Ecuador": {"space": "90010106163", "folder": "90175037512"},
    "PSFR": {"space": "90010110032", "folder": "90175032136"},
    "Chile": {"space": "90170793556", "folder": "90175036890"},
}

FALLBACK_CONFIG = {
    "GGPx": {"active": "901711483884"},
    "Ecuador": {"active": "901711409692"},
    "PSFR": {"active": "901711345388"},
    "Chile": {"active": "901711409637"},
}

CF = {
    "OWNER": "8fbf5654-f0a7-49d2-96d4-f3043bbe7ab8",
    "SP": "0e6cb56a-62f9-4030-861b-8dc0c267562c",
    "EXPIRED": "0e7496df-fa6a-4d48-a11e-388e6bec4e64",
    "QA_INST": "635cbeda-8402-4d5c-85c6-d31f9978d1de",
    "DEPLOY_INST": "b77d62dc-6dc4-4c0a-8e96-70cbabdbd8b6",
    "PR_LINK": "5da025a9-f560-47e1-899c-56969d04c392",
    "ROUNDS": "0d80f2be-006f-4ca9-9e14-13dddc7c0462",
    "PRIORITY": "970395ae-22be-45d1-952a-83e9269715b4",
}

# Roster del equipo — SOLO para el modo preview de manager (--dev). El dev normal se
# auto-detecta por su token y NO necesita este roster. Son nombres + IDs de ClickUp (no secretos).
TEAM = {
    "89212278": "Omar G.",
    "67211379": "Mario A.",
    "67211381": "Diego D.",
    "67211376": "Christian C.",
    "67211378": "Damian L.",
    "89342644": "Juan M.",
    "156068535": "José F.",
}

STATUS_DONE = ["in sprint", "ready for deployment", "done", "closed", "merge to sprint"]
STATUS_QA = ["qa"]
STATUS_MERGE_DEV = ["merge to dev/test"]
STATUS_PROGRESS = ["in progress"]
STATUS_TODO = ["to do"]
STATUS_BLOCKED = ["blocked"]
STATUS_OPEN = ["open"]
SPRINT_NAME_RE = re.compile(r"sprint\s*(\d+).*\((\d{1,2})/(\d{1,2})\s*-\s*(\d{1,2})/(\d{1,2})\)", re.I)

# ── Metas del lineamiento (las mismas que audita el Guardián) ──────────────────
QA_INST_MIN_CHARS = 50
META_DOC = 80.0          # % de mis QA/Done con docs completas
META_VENCIDAS = 15.0     # % de mis no-terminadas vencidas (máximo)
META_ROUNDS = 1.0        # rounds de QA promedio (máximo)
RATIO_OK = (70.0, 120.0)  # ratio gastado/estimado saludable
TIS_MAX = {"to do": 10.0, "in progress": 10.0, "qa": 3.0, "merge to dev/test": 3.0}
SAT_TASKS = 15
SAT_HOURS = 80.0
SAT_PROJECTS = 3


# ── Helpers de entorno / API (derivados de tdd_report.py) ─────────────────────
def load_env(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            key, val = line.split("=", 1)
            os.environ.setdefault(key.strip(), val.strip())


def api_get(endpoint: str, headers: dict, silent_ecodes: tuple = ()) -> dict:
    url = f"{BASE_URL}/{endpoint}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as err:
        body = err.read().decode("utf-8", errors="replace")
        if silent_ecodes:
            try:
                if json.loads(body).get("ECODE", "") in silent_ecodes:
                    return {}
            except Exception:
                pass
        print(f"[ERROR] GET {endpoint} -> HTTP {err.code}: {body}", file=sys.stderr)
    except urllib.error.URLError as err:
        print(f"[ERROR] GET {endpoint} -> URL error: {err}", file=sys.stderr)
    except Exception as err:
        print(f"[ERROR] GET {endpoint} -> {err}", file=sys.stderr)
    return {}


def fetch_tis_bulk(task_ids: list, headers: dict) -> dict:
    if not task_ids:
        return {}
    result = {}
    for i in range(0, len(task_ids), 100):
        batch = task_ids[i : i + 100]
        ids_param = "&".join(f"task_ids[]={tid}" for tid in batch)
        url = f"{BASE_URL}/task/bulk_time_in_status/task_ids?{ids_param}"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req) as response:
                result.update(json.loads(response.read().decode("utf-8")))
        except urllib.error.HTTPError as err:
            err.read()
            for tid in batch:
                time.sleep(0.1)
                tis = api_get(f"task/{tid}/time_in_status", headers, silent_ecodes=("OAUTH_161",))
                if tis:
                    result[tid] = tis
        except Exception as err:
            print(f"[ERROR] bulk_time_in_status -> {err}", file=sys.stderr)
        time.sleep(0.2)
    return result


def fetch_all_tasks(list_id: str, headers: dict) -> list:
    tasks = []
    page = 0
    while True:
        data = api_get(f"list/{list_id}/task?include_closed=true&subtasks=true&page={page}", headers)
        batch = data.get("tasks", [])
        tasks.extend(batch)
        if data.get("last_page") or not batch:
            break
        page += 1
    return tasks


def get_cf_val(task: dict, fid: str):
    for field in task.get("custom_fields", []):
        if field.get("id") == fid:
            return field.get("value")
    return None


def owner_id_of(task: dict) -> str:
    owner_val = get_cf_val(task, CF["OWNER"])
    if owner_val:
        if isinstance(owner_val, list) and owner_val:
            return str(owner_val[0].get("id"))
        if isinstance(owner_val, dict):
            return str(owner_val.get("id"))
    return ""


def is_mine(task: dict, my_id: str) -> bool:
    """Regla Owner > Assignee: una tarea es 'mía' si soy el Owner; si no hay Owner,
    si estoy entre los assignees."""
    owner = owner_id_of(task)
    if owner:
        return owner == my_id
    return any(str(a.get("id")) == my_id for a in task.get("assignees", []))


def parse_sprint(list_obj: dict, year: int) -> "dict | None":
    match = SPRINT_NAME_RE.search(list_obj.get("name", ""))
    if not match:
        return None
    _, sm, sd, em, ed = match.groups()
    start = datetime(year, int(sm), int(sd), tzinfo=timezone.utc)
    end = datetime(year, int(em), int(ed), tzinfo=timezone.utc)
    return {"id": list_obj["id"], "name": list_obj["name"], "start": start.isoformat(), "end": end.isoformat()}


def discover_sprints(now: datetime, headers: dict) -> dict:
    month, year = now.month, now.year
    config = {"generated_at": now.isoformat(), "year": year, "month": month, "projects": {}}
    for pname, ids in PROJECTS.items():
        lists = api_get(f"folder/{ids['folder']}/list", headers).get("lists", [])
        parsed = [p for p in (parse_sprint(lst, year) for lst in lists) if p]
        parsed.sort(key=lambda x: datetime.fromisoformat(x["start"]))
        active = None
        for sprint in parsed:
            if datetime.fromisoformat(sprint["start"]) <= now <= datetime.fromisoformat(sprint["end"]):
                active = sprint
                break
        if not active:
            for sprint in parsed:
                if datetime.fromisoformat(sprint["start"]).month == month or datetime.fromisoformat(sprint["end"]).month == month:
                    active = sprint
                    break
        if not active:
            active = {"id": FALLBACK_CONFIG[pname]["active"], "name": "Fallback active", "start": now.isoformat(), "end": now.isoformat()}
        config["projects"][pname] = {"active": active}
    return config


def ensure_config(now: datetime, headers: dict, force: bool = False) -> dict:
    if force or not CONFIG_PATH.exists():
        config = discover_sprints(now, headers)
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return config
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    if config.get("month") != now.month or config.get("year") != now.year:
        config = discover_sprints(now, headers)
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
    return config


def whoami(headers: dict) -> dict:
    """Auto-detecta el perfil del dueño del token."""
    user = api_get("user", headers).get("user", {})
    return {"id": str(user.get("id", "")), "name": user.get("username") or user.get("email") or "Yo"}


def resolve_dev(value: str) -> "dict | None":
    """Resuelve --dev a un perfil. Acepta un ID numérico o un nombre (o parte) del roster TEAM."""
    value = value.strip()
    if value.isdigit():
        return {"id": value, "name": TEAM.get(value, f"id {value}")}
    low = value.lower()
    for uid, name in TEAM.items():
        if low in name.lower():
            return {"id": uid, "name": name}
    return None


# ── Bloque AI (opcional) ──────────────────────────────────────────────────────
_NOISE_PREFIXES = (
    "Keychain initialization", "Using FileKeychain", "Loaded cached credentials",
    "Registering notification", "Scheduling MCP", "Executing MCP", "MCP context refresh",
    "Error stating path", "ENAMETOOLONG", "Server '", "[ERROR]", "Attempt ",
    "GaxiosError", "at Gaxios", "at process.", "at async ", "at ClientRequest",
    "at TLSSocket", "FetchError",
)


def _strip_noise(raw: str) -> str:
    return "\n".join(
        line for line in raw.splitlines()
        if not any(line.strip().startswith(p) or p in line for p in _NOISE_PREFIXES)
    )


def _parse_ai_blocks(raw: str) -> dict:
    cleaned = _strip_noise(raw)
    result = {}
    m = re.search(r'\[COACHING\](.*?)\[/COACHING\]', cleaned, re.DOTALL)
    if m:
        result["COACHING"] = m.group(1).strip()
    return result


def _build_ai_cmd(ai_cmd: str, prompt: str) -> list:
    if ai_cmd == "gemini":
        return ["gemini", "--allowed-mcp-server-names", "", "-p", prompt]
    return [ai_cmd, "-p", prompt]


def run_ai_analysis(ctx_text: str, report_path: Path) -> None:
    ai_cmd = None
    for candidate in ("gemini", "claude"):
        if shutil.which(candidate):
            ai_cmd = candidate
            break
    if not ai_cmd:
        print("No se encontró CLI de AI (gemini/claude). Se omite el coaching.")
        return
    prompt = (
        "Eres un coach técnico de un desarrollador. Lee sus datos del sprint en el input. "
        "Responde SOLO en español usando EXACTAMENTE el formato delimitado. "
        "Devuelve UN bloque [COACHING]...[/COACHING] con 3 a 5 acciones concretas y priorizadas de "
        "'qué ajustar hoy' según sus datos. Sé directo y accionable. NO modifiques datos numéricos. NO crees scripts."
    )
    cmd = _build_ai_cmd(ai_cmd, prompt)
    print(f"Generando coaching con {ai_cmd} (puede tardar)...")
    try:
        result = subprocess.run(cmd, input=ctx_text, capture_output=True, text=True, timeout=180)
        raw = (result.stdout or "") + (result.stderr or "")
        blocks = _parse_ai_blocks(raw)
        if not blocks:
            print(f"[WARN] {ai_cmd} no devolvió un bloque [COACHING] reconocible.")
            return
        current = report_path.read_text(encoding="utf-8")
        current = current.replace("<!-- AI:COACHING -->", blocks["COACHING"])
        report_path.write_text(current, encoding="utf-8")
        print(f"Coaching ({ai_cmd}) agregado al reporte.")
    except subprocess.TimeoutExpired:
        print(f"[WARN] {ai_cmd} agotó el tiempo (180s). Se omite el coaching.")
    except Exception as err:
        print(f"[WARN] Coaching falló ({ai_cmd}): {err}")


# ── Utilidades ────────────────────────────────────────────────────────────────
def _trunc(name: str, n: int = 34) -> str:
    return (name[: n - 1] + "…") if len(name) > n else name


def _pct(part: int, whole: int) -> float:
    return (part / whole * 100) if whole else 100.0


def _mark(ok: bool) -> str:
    return "✅" if ok else "⚠️"


# ── Análisis personal (determinístico) ────────────────────────────────────────
def analyze_me(now: datetime, headers: dict, config: dict, me: dict) -> dict:
    my_id = me["id"]
    now_ms = now.timestamp() * 1000
    today_local = datetime.now().date()
    mine = []  # (task, project)
    for pname, pconf in config["projects"].items():
        for t in fetch_all_tasks(pconf["active"]["id"], headers):
            st = t.get("status", {}).get("status", "").lower()
            if st in STATUS_OPEN:
                continue
            if is_mine(t, my_id):
                mine.append((t, pname))

    pulso = {"total": 0, "done": 0, "qa": 0, "merge": 0, "prog": 0, "todo": 0, "blocked": 0}
    proys = set()
    est_total = gast_total = 0.0
    vencidas, no_doc, no_est = [], [], []
    rounds_vals = []
    tis_violators = []
    inprogress_count = 0

    for t, pname in mine:
        st = t.get("status", {}).get("status", "").lower()
        proys.add(pname)
        pulso["total"] += 1
        if st in STATUS_DONE:
            pulso["done"] += 1
        elif st in STATUS_QA:
            pulso["qa"] += 1
        elif st in STATUS_MERGE_DEV:
            pulso["merge"] += 1
        elif st in STATUS_PROGRESS:
            pulso["prog"] += 1
        elif st in STATUS_TODO:
            pulso["todo"] += 1
        elif st in STATUS_BLOCKED:
            pulso["blocked"] += 1

        est_h = float((t.get("time_estimate") or 0) / 3600000.0)
        spent_h = float((t.get("time_spent") or 0) / 3600000.0)
        est_total += est_h
        gast_total += spent_h

        # Sin estimar (in progress / to do, 0h)
        if st not in STATUS_DONE and est_h == 0 and st in (STATUS_PROGRESS + STATUS_TODO):
            no_est.append({"id": t["id"], "name": t["name"], "proj": pname, "status": t["status"]["status"]})

        # Vencidas: solo tareas en 'in progress' (misma regla que el guardián)
        if st in STATUS_PROGRESS:
            inprogress_count += 1
            expired = float(get_cf_val(t, CF["EXPIRED"]) or 0)
            due = t.get("due_date")
            due_past = datetime.fromtimestamp(int(due) / 1000).date() <= today_local if due else False
            if expired > 0 or due_past:
                vencidas.append({"id": t["id"], "name": t["name"], "proj": pname, "status": t["status"]["status"]})

        # Documentación (en QA/Done/Merge→Dev)
        if st in STATUS_DONE or st in STATUS_QA or st in STATUS_MERGE_DEV:
            qa_inst = get_cf_val(t, CF["QA_INST"]) or ""
            pr_link = get_cf_val(t, CF["PR_LINK"]) or ""
            deploy_inst = get_cf_val(t, CF["DEPLOY_INST"]) or ""
            missing = []
            if not (isinstance(qa_inst, str) and len(qa_inst.strip()) >= QA_INST_MIN_CHARS):
                missing.append("Q")
            if not pr_link:
                missing.append("P")
            if not deploy_inst:
                missing.append("D")
            if missing:
                no_doc.append({"id": t["id"], "name": t["name"], "proj": pname,
                               "status": t["status"]["status"], "miss": missing})

        # Calidad (rounds de QA)
        rounds_raw = get_cf_val(t, CF["ROUNDS"]) or {}
        rounds_val = int((rounds_raw.get("current", 0) if isinstance(rounds_raw, dict) else rounds_raw) or 0)
        if rounds_val > 0:
            rounds_vals.append(rounds_val)

    # Documentación: elegibles = mis QA/Done/Merge
    doc_eligible = pulso["done"] + pulso["qa"] + pulso["merge"]
    doc_ok = doc_eligible - len(no_doc)
    doc_pct = _pct(doc_ok, doc_eligible)

    # Plazos: % de vencidas sobre tareas en 'in progress' (0 si no hay en progreso)
    venc_pct = (len(vencidas) / inprogress_count * 100) if inprogress_count else 0.0

    # Tracking
    ratio = (gast_total / est_total * 100) if est_total else 0.0

    # Calidad
    avg_rounds = (sum(rounds_vals) / len(rounds_vals)) if rounds_vals else 0.0

    # Time-in-status de mis no-terminadas
    nondone_tasks = {t["id"]: (t, p) for t, p in mine
                     if t.get("status", {}).get("status", "").lower() not in STATUS_DONE}
    tis_bulk = fetch_tis_bulk(list(nondone_tasks.keys()), headers)
    for tid, (t, pname) in nondone_tasks.items():
        st = t.get("status", {}).get("status", "").lower()
        if st not in TIS_MAX:
            continue
        days = 0.0
        tis = tis_bulk.get(tid)
        if tis:
            by_minute = tis.get("current_status", {}).get("total_time", {}).get("by_minute", 0)
            days = round(float(by_minute) / 1440.0, 1)
        if days <= 0:
            start_ms = int(t.get("date_last_status_change") or 0) or int(t.get("date_created") or 0)
            if start_ms:
                days = round((now_ms - start_ms) / 86400000.0, 1)
        if days > TIS_MAX[st]:
            tis_violators.append({"id": tid, "name": t["name"], "proj": pname,
                                  "status": t["status"]["status"], "days": days, "max": TIS_MAX[st]})
    tis_violators.sort(key=lambda x: x["days"], reverse=True)

    # Señales de saturación
    sat_reasons = []
    if pulso["total"] > SAT_TASKS:
        sat_reasons.append(f">{SAT_TASKS} tareas activas ({pulso['total']})")
    if est_total > SAT_HOURS:
        sat_reasons.append(f">{int(SAT_HOURS)}h estimadas ({est_total:.0f}h)")
    if len(proys) >= SAT_PROJECTS:
        sat_reasons.append(f"{len(proys)} proyectos simultáneos")

    return {
        "generated_at": now.isoformat(),
        "me": me,
        "proys": sorted(proys),
        "pulso": pulso,
        "est_total": round(est_total, 1),
        "gast_total": round(gast_total, 1),
        "ratio": round(ratio, 1),
        "doc": {"pct": round(doc_pct, 1), "ok": doc_ok, "eligible": doc_eligible, "offenders": no_doc},
        "plazos": {"venc_pct": round(venc_pct, 1), "inprogress": inprogress_count, "vencidas": vencidas},
        "estimacion": {"offenders": no_est},
        "calidad": {"avg_rounds": round(avg_rounds, 2), "n": len(rounds_vals)},
        "tis_violators": tis_violators,
        "saturacion": sat_reasons,
    }


# ── Reporte markdown ──────────────────────────────────────────────────────────
def build_report(d: dict, today_str: str) -> str:
    R = []
    me = d["me"]
    p = d["pulso"]
    R.append(f"# Mi Diagnóstico — {me['name']} — {today_str}")
    R.append(f"## Tareas en el sprint activo · Proyectos: {', '.join(d['proys']) or '—'}")
    R.append("")
    R.append("---")
    R.append("")

    if p["total"] == 0:
        R.append("> No se encontraron tareas tuyas en el sprint activo de los 4 proyectos.")
        R.append("> Verifica que: (1) tu token de ClickUp es el correcto, y (2) eres **Owner** "
                 "(o assignee) de tareas en el sprint de este mes. Si eres líder/manager, es normal.")
        R.append("")
        R.append("---")
        R.append("")

    # Pulso
    R.append("### Mi pulso")
    R.append("")
    R.append("```")
    R.append(f"Total activas : {p['total']}")
    R.append(f"Terminadas    : {p['done']}")
    R.append(f"En QA         : {p['qa']}")
    R.append(f"Merge→Dev     : {p['merge']}")
    R.append(f"En progreso   : {p['prog']}")
    R.append(f"Pendientes    : {p['todo']}")
    R.append(f"Bloqueadas    : {p['blocked']}")
    R.append(f"Estimado/Gastado : {d['est_total']}h / {d['gast_total']}h")
    R.append("```")
    R.append("")

    # Cumplimiento vs metas
    doc = d["doc"]; plazos = d["plazos"]; cal = d["calidad"]
    has_est = d["est_total"] > 0
    doc_ok = doc["pct"] >= META_DOC
    venc_ok = plazos["venc_pct"] <= META_VENCIDAS
    # Sin horas estimadas el ratio no aplica: no se marca como incumplimiento.
    ratio_ok = (RATIO_OK[0] <= d["ratio"] <= RATIO_OK[1]) if has_est else True
    rounds_ok = cal["avg_rounds"] <= META_ROUNDS
    est_ok = not d["estimacion"]["offenders"]
    sat_ok = not d["saturacion"]
    tis_ok = not d["tis_violators"]

    R.append("### Cumplimiento del lineamiento")
    R.append("")
    R.append("```")
    h = f"{'Dimensión':<26} {'Tú':>10}  {'Meta':<12} {'Estado'}"
    R.append(h)
    R.append("─" * len(h))
    R.append(f"{'Documentación QA/Done':<26} {doc['pct']:>9.0f}%  {'≥80%':<12} {_mark(doc_ok)}")
    R.append(f"{'Plazos (vencidas)':<26} {plazos['venc_pct']:>9.0f}%  {'≤15%':<12} {_mark(venc_ok)}")
    ratio_disp = f"{d['ratio']:.0f}%" if has_est else "N/A"
    R.append(f"{'Tracking (gast/est)':<26} {ratio_disp:>10}  {'70-120%':<12} {_mark(ratio_ok)}")
    R.append(f"{'Calidad (rounds QA)':<26} {cal['avg_rounds']:>10.2f}  {'≤1.0':<12} {_mark(rounds_ok)}")
    R.append(f"{'Estimación (sin SP)':<26} {len(d['estimacion']['offenders']):>10}  {'0 tareas':<12} {_mark(est_ok)}")
    R.append(f"{'Tiempo en estado':<26} {len(d['tis_violators']):>10}  {'0 tareas':<12} {_mark(tis_ok)}")
    R.append(f"{'Saturación':<26} {len(d['saturacion']):>10}  {'0 señales':<12} {_mark(sat_ok)}")
    R.append("```")
    R.append("")
    R.append("---")
    R.append("")

    # Detalle accionable
    R.append("### Qué revisar")
    R.append("")

    R.append("#### Documentación faltante (QA/Done)")
    R.append("")
    if doc["offenders"]:
        R.append("```")
        R.append("Leyenda: Q=QA Instructions (≥50 chars)  P=PR/MR Link  D=Deploy Instructions")
        R.append("")
        hh = f"{'ID':<11} {'Tarea':<34} {'Proy':<8} {'Estado':<16} {'Falta':<6}"
        R.append(hh)
        R.append("─" * len(hh))
        for a in doc["offenders"]:
            R.append(f"{a['id']:<11} {_trunc(a['name']):<34} {a['proj']:<8} {a['status'][:16]:<16} {','.join(a['miss']):<6}")
        R.append("```")
    else:
        R.append("Toda tu documentación está completa. ✅")
    R.append("")

    R.append("#### Tareas vencidas (in progress)")
    R.append("")
    if plazos["vencidas"]:
        R.append("```")
        hh = f"{'ID':<11} {'Tarea':<34} {'Proy':<8} {'Estado':<16}"
        R.append(hh)
        R.append("─" * len(hh))
        for a in plazos["vencidas"]:
            R.append(f"{a['id']:<11} {_trunc(a['name']):<34} {a['proj']:<8} {a['status'][:16]:<16}")
        R.append("```")
    else:
        R.append("Sin tareas vencidas. ✅")
    R.append("")

    R.append("#### Tareas sin estimar (Sprint Points / horas)")
    R.append("")
    if d["estimacion"]["offenders"]:
        R.append("```")
        hh = f"{'ID':<11} {'Tarea':<34} {'Proy':<8} {'Estado':<16}"
        R.append(hh)
        R.append("─" * len(hh))
        for a in d["estimacion"]["offenders"]:
            R.append(f"{a['id']:<11} {_trunc(a['name']):<34} {a['proj']:<8} {a['status'][:16]:<16}")
        R.append("```")
    else:
        R.append("Todas tus tareas en curso están estimadas. ✅")
    R.append("")

    R.append("#### Tareas estancadas (exceden el tiempo máximo en su estado)")
    R.append("")
    if d["tis_violators"]:
        R.append("Máximos: TO DO/IN PROGRESS ≤10d · QA ≤3d · Merge→Dev ≤3d.")
        R.append("")
        R.append("```")
        hh = f"{'ID':<11} {'Tarea':<34} {'Proy':<8} {'Estado':<16} {'Días':>6} {'Máx':>5}"
        R.append(hh)
        R.append("─" * len(hh))
        for a in d["tis_violators"]:
            R.append(f"{a['id']:<11} {_trunc(a['name']):<34} {a['proj']:<8} {a['status'][:16]:<16} {a['days']:>5.1f}d {a['max']:>4.0f}d")
        R.append("```")
    else:
        R.append("Ninguna tarea estancada. ✅")
    R.append("")

    if d["saturacion"]:
        R.append("#### Señales de saturación")
        R.append("")
        for s in d["saturacion"]:
            R.append(f"- ⚠️ {s}")
        R.append("")

    R.append("---")
    R.append("")
    R.append("### Coaching — qué ajustar hoy")
    R.append("")
    R.append("<!-- AI:COACHING -->")
    R.append("")
    R.append("---")
    R.append(f"*Generado el {today_str} por Dev-Check. Corre con `--analyze` para el coaching AI.*")
    return "\n".join(R)


def build_analysis_context(report_text: str, d: dict, today: str) -> str:
    subset = {
        "me": d["me"]["name"],
        "pulso": d["pulso"],
        "ratio": d["ratio"],
        "doc_pct": d["doc"]["pct"],
        "venc_pct": d["plazos"]["venc_pct"],
        "avg_rounds": d["calidad"]["avg_rounds"],
        "sin_estimar": len(d["estimacion"]["offenders"]),
        "tareas_estancadas": len(d["tis_violators"]),
        "saturacion": d["saturacion"],
        "proys": d["proys"],
    }
    return "\n".join([
        f"# Contexto de Coaching del Dev — {d['me']['name']} — {today}",
        "",
        "## INSTRUCCIONES PARA EL MODELO",
        "Eres un coach técnico. Lee los datos del desarrollador y responde SOLO en español.",
        "DEBES devolver EXACTAMENTE este formato:",
        "```",
        "[COACHING]",
        "1. acción concreta y priorizada",
        "2. acción concreta y priorizada",
        "3. acción concreta y priorizada",
        "[/COACHING]",
        "```",
        "Da entre 3 y 5 acciones. Sé directo y accionable. NO alteres datos numéricos.",
        "",
        "## MI REPORTE",
        report_text,
        "",
        "## MIS MÉTRICAS (JSON)",
        json.dumps(subset, indent=2, ensure_ascii=False),
    ])


def execute(now: datetime, headers: dict, config: dict, do_analyze: bool, dev_override: "dict | None" = None) -> None:
    today_str = now.strftime("%Y-%m-%d")
    if dev_override:
        me = dev_override
        print(f"Modo preview (manager): perfil {me['name']} (id {me['id']})")
    else:
        me = whoami(headers)
        if not me["id"]:
            print("ERROR: no se pudo detectar tu usuario desde el token. Revisa tu CLICKUP_API_KEY.", file=sys.stderr)
            sys.exit(1)
        print(f"Perfil detectado: {me['name']} (id {me['id']})")

    d = analyze_me(now, headers, config, me)
    report_text = build_report(d, today_str)
    report_path = ROOT / REPORT_NAME.format(date=today_str)
    report_path.write_text(report_text, encoding="utf-8")
    (ROOT / DATA_NAME.format(date=today_str)).write_text(
        json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Reporte generado: {report_path.name}")

    if do_analyze:
        ctx_text = build_analysis_context(report_text, d, today_str)
        (ROOT / ANALYSIS_CONTEXT_NAME).write_text(ctx_text, encoding="utf-8")
        run_ai_analysis(ctx_text, report_path)
    else:
        # Sin AI: dejar una nota en lugar del placeholder para que el reporte quede limpio.
        clean = report_path.read_text(encoding="utf-8").replace(
            "<!-- AI:COACHING -->",
            "_(Corre con `--analyze` para obtener 3-5 acciones personalizadas con AI.)_")
        report_path.write_text(clean, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description="Dev-Check — autoayuda de cumplimiento para desarrolladores CODELAR.")
    parser.add_argument("mode", nargs="?", default="run", choices=["run"], help="Modo run por defecto.")
    parser.add_argument("--setup", action="store_true", help="Descubre y cachea sprints (config.json).")
    parser.add_argument("--validate", action="store_true", help="Valida API y perfil.")
    parser.add_argument("--analyze", action="store_true", help="Agrega coaching AI (más lento).")
    parser.add_argument("--dev", default=None, metavar="NOMBRE|ID",
                        help="Modo preview (manager): analiza el perfil de otro dev en vez del tuyo.")
    args = parser.parse_args()

    load_env(ROOT / ".env")
    api_key = os.environ.get("CLICKUP_API_KEY")
    if not api_key:
        print("ERROR: CLICKUP_API_KEY no configurada. Copia .env.example a .env y pon tu token.", file=sys.stderr)
        sys.exit(1)
    headers = {"Authorization": api_key, "Content-Type": "application/json"}
    now = datetime.now(timezone.utc)

    if args.setup:
        config = discover_sprints(now, headers)
        CONFIG_PATH.write_text(json.dumps(config, indent=2), encoding="utf-8")
        print(f"Config creada: {CONFIG_PATH.name}")
        return

    if args.validate:
        me = whoami(headers)
        if not me["id"]:
            print("Validación con error: no se detectó el usuario del token.", file=sys.stderr)
            sys.exit(2)
        ensure_config(now, headers, force=False)
        print(f"Validación OK: API accesible, perfil {me['name']}.")
        return

    dev_override = None
    if args.dev:
        dev_override = resolve_dev(args.dev)
        if not dev_override:
            print(f"ERROR: no reconozco al dev '{args.dev}'. Opciones: {', '.join(TEAM.values())}", file=sys.stderr)
            sys.exit(2)

    config = ensure_config(now, headers, force=False)
    execute(now, headers, config, do_analyze=args.analyze, dev_override=dev_override)


if __name__ == "__main__":
    main()
