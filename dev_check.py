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
from datetime import datetime, timedelta, timezone
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
# Tiempo-en-estado por ROL: cada quien se mide solo en los estados que controla.
# El dev no es responsable de mover tareas fuera de QA (eso es del QA tester).
QA_TEAM = {"89342644": "Juan M.", "156068535": "José F."}   # perfiles QA (id -> nombre)
QA_IDS = set(QA_TEAM)
DEV_TIS_MAX = {"to do": 10.0, "in progress": 10.0, "merge to dev/test": 3.0}
QA_TIS_MAX = {"qa": 3.0}
QA_QUEUE_MAX = 5            # cola de QA que indica cuello de botella
# Capacidad real (igual que el guardián): días hábiles colombianos − reuniones.
HOURS_PER_DAY = 8.0
MEETING_HOURS_SPRINT = 50.0
CO_HOLIDAYS_2026 = frozenset({
    "2026-01-01", "2026-01-12", "2026-03-23", "2026-04-02", "2026-04-03", "2026-05-01",
    "2026-05-18", "2026-06-08", "2026-06-15", "2026-06-29", "2026-07-20", "2026-08-07",
    "2026-08-17", "2026-10-12", "2026-11-02", "2026-11-16", "2026-12-08", "2026-12-25",
})
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


def business_days(start: datetime, end: datetime) -> int:
    """Días hábiles (L-V) entre start y end inclusive, descontando festivos CO 2026."""
    if end < start:
        return 0
    d, last, count = start.date(), end.date(), 0
    while d <= last:
        if d.weekday() < 5 and d.isoformat() not in CO_HOLIDAYS_2026:
            count += 1
        d += timedelta(days=1)
    return count


# ── Análisis personal (determinístico) ────────────────────────────────────────
def analyze_me(now: datetime, headers: dict, config: dict, me: dict) -> dict:
    my_id = me["id"]
    is_qa = my_id in QA_IDS
    role = "QA" if is_qa else "Dev"
    tis_max = QA_TIS_MAX if is_qa else DEV_TIS_MAX   # cada rol se mide en sus estados
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
    load_remaining = 0.0   # Σ(estimado − gastado) de tareas no-terminadas
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
        if st not in STATUS_DONE:
            load_remaining += max(0.0, est_h - spent_h)   # horas que aún te faltan

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
        if st not in tis_max:   # solo los estados que controla este rol
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
        if days > tis_max[st]:
            tis_violators.append({"id": tid, "name": t["name"], "proj": pname,
                                  "status": t["status"]["status"], "days": days, "max": tis_max[st]})
    tis_violators.sort(key=lambda x: x["days"], reverse=True)

    # Capacidad real: días hábiles colombianos restantes × 8h − reuniones prorrateadas
    starts = [datetime.fromisoformat(p["active"]["start"]) for p in config["projects"].values()]
    ends = [datetime.fromisoformat(p["active"]["end"]) for p in config["projects"].values()]
    sprint_start = min(starts) if starts else now
    sprint_end = max(ends) if ends else now
    dias_totales = business_days(sprint_start, sprint_end)
    dias_restantes = business_days(now, sprint_end)
    capacidad = max(0.0, dias_restantes * HOURS_PER_DAY
                    - MEETING_HOURS_SPRINT * (dias_restantes / dias_totales if dias_totales else 0.0))

    # Señales de saturación
    sat_reasons = []
    if pulso["total"] > SAT_TASKS:
        sat_reasons.append(f">{SAT_TASKS} tareas activas ({pulso['total']})")
    if len(proys) >= SAT_PROJECTS:
        sat_reasons.append(f"{len(proys)} proyectos simultáneos")

    return {
        "generated_at": now.isoformat(),
        "me": me,
        "role": role,
        "tis_max": tis_max,
        "proys": sorted(proys),
        "pulso": pulso,
        "est_total": round(est_total, 1),
        "gast_total": round(gast_total, 1),
        "load_remaining": round(load_remaining, 1),
        "capacidad": round(capacidad, 1),
        "dias_restantes": dias_restantes,
        "dias_totales": dias_totales,
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
    cap_ok = d["load_remaining"] <= d["capacidad"]

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
    margen = d["capacidad"] - d["load_remaining"]
    R.append(f"{'Capacidad (margen libre)':<26} {f'{margen:.0f}h':>10}  {'≥ 0h':<12} {_mark(cap_ok)}")
    R.append(f"{'Saturación':<26} {len(d['saturacion']):>10}  {'0 señales':<12} {_mark(sat_ok)}")
    R.append("```")
    R.append("")
    base = (f"({d['dias_restantes']} de {d['dias_totales']} días hábiles del sprint, "
            f"L-V menos festivos CO, menos reuniones)")
    if margen >= 0:
        R.append(f"_Capacidad: **{margen:.0f}h libres** — tienes **{d['load_remaining']:.0f}h** de trabajo "
                 f"pendiente y un cupo de **{d['capacidad']:.0f}h** en lo que queda del sprint {base}. "
                 f"Puedes asumir ~{margen:.0f}h más._")
    else:
        R.append(f"_Capacidad: **sobrecargado por {abs(margen):.0f}h** — tienes **{d['load_remaining']:.0f}h** "
                 f"pendientes pero solo **{d['capacidad']:.0f}h** de cupo en lo que queda del sprint {base}. "
                 f"Hay que descargar ~{abs(margen):.0f}h._")
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

    maxes = " · ".join(f"{k.upper()} ≤{int(v)}d" for k, v in d["tis_max"].items())
    R.append(f"#### Tareas estancadas en los estados que controlas ({d['role']})")
    R.append("")
    if d["tis_violators"]:
        R.append(f"Máximos para tu rol ({d['role']}): {maxes}.")
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
        "capacidad_disp_h": d["capacidad"],
        "carga_pendiente_h": d["load_remaining"],
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


# ── Vista QA (filtra por cola/assignee, no por Owner) ─────────────────────────
def analyze_qa(now: datetime, headers: dict, config: dict, me: dict) -> dict:
    """Análisis para un perfil QA: su cola de revisión, estancadas, tareas no
    testeables (sin QA Instructions), hallazgos (rounds) generados y tiempo en QA."""
    my_id = me["id"]
    now_ms = now.timestamp() * 1000
    queue, no_qa_inst, tasks_with_rounds = [], [], []
    tracked_qa_h = 0.0
    proys = set()
    qa_total_sprint = 0
    # Ventana del sprint por proyecto (para acotar los hallazgos a este sprint)
    windows = {p: (datetime.fromisoformat(pc["active"]["start"]).date(),
                   datetime.fromisoformat(pc["active"]["end"]).date())
               for p, pc in config["projects"].items()}

    for pname, pconf in config["projects"].items():
        for t in fetch_all_tasks(pconf["active"]["id"], headers):
            st = t.get("status", {}).get("status", "").lower()
            if st in STATUS_OPEN:
                continue
            assignee_ids = {str(a.get("id")) for a in t.get("assignees", [])}
            if st in STATUS_QA:
                qa_total_sprint += 1
                if my_id in assignee_ids:
                    proys.add(pname)
                    qa_inst = get_cf_val(t, CF["QA_INST"]) or ""
                    has_qa = isinstance(qa_inst, str) and len(qa_inst.strip()) >= QA_INST_MIN_CHARS
                    queue.append({"id": t["id"], "name": t["name"], "proj": pname, "has_qa": has_qa, "days": 0.0})
                    if not has_qa:
                        no_qa_inst.append({"id": t["id"], "name": t["name"], "proj": pname})
            # Tiempo registrado en tareas "Revisión integral de QA" asignadas a mí
            if "revisión integral" in t.get("name", "").lower() and my_id in assignee_ids:
                tracked_qa_h += float((t.get("time_spent") or 0) / 3600000.0)
            # Tareas con rounds>0 (para atribuir hallazgos por comentarios)
            rounds_raw = get_cf_val(t, CF["ROUNDS"]) or {}
            rounds_val = int((rounds_raw.get("current", 0) if isinstance(rounds_raw, dict) else rounds_raw) or 0)
            if rounds_val > 0:
                tasks_with_rounds.append((t, pname, rounds_val))

    # Días en QA para mi cola (time_in_status)
    tis = fetch_tis_bulk([q["id"] for q in queue], headers)
    for q in queue:
        info = tis.get(q["id"])
        if info:
            by_min = info.get("current_status", {}).get("total_time", {}).get("by_minute", 0)
            q["days"] = round(float(by_min) / 1440.0, 1)
    queue.sort(key=lambda x: x["days"], reverse=True)
    estancadas = [q for q in queue if q["days"] > QA_TIS_MAX["qa"]]

    # Hallazgos (rounds) atribuidos a mí, ACOTADOS al sprint: solo cuentan si mi
    # comentario cae dentro de la ventana del sprint. El hallazgo mostrado es mi
    # comentario más temprano en ventana (la devolución, no la re-validación).
    my_rounds = 0
    hallazgos = []
    for t, pname, rv in tasks_with_rounds:
        s_date, e_date = windows.get(pname, (None, None))
        comments = api_get(f"task/{t['id']}/comment", headers).get("comments", [])
        by_qa = {}
        for c in comments:
            uid = str(c.get("user", {}).get("id", ""))
            if uid not in QA_TEAM:
                continue
            cdate = datetime.fromtimestamp(int(c.get("date", 0)) / 1000, tz=timezone.utc).date()
            if s_date and e_date and s_date <= cdate <= e_date:
                by_qa.setdefault(uid, []).append((cdate, (c.get("comment_text", "") or "").replace("\n", " ").strip()))
        if by_qa and max(by_qa, key=lambda u: len(by_qa[u])) == my_id:
            my_rounds += rv
            first_date, first_text = sorted(by_qa[my_id], key=lambda x: x[0])[0]
            returned_to = TEAM.get(owner_id_of(t)) or "?"
            hallazgos.append({"id": t["id"], "name": t["name"], "proj": pname,
                              "returned_to": returned_to, "date": first_date.isoformat(), "comment": first_text})
        time.sleep(0.15)
    hallazgos.sort(key=lambda h: h["date"])

    return {
        "generated_at": now.isoformat(),
        "me": me,
        "role": "QA",
        "proys": sorted(proys),
        "queue": queue,
        "qa_total_sprint": qa_total_sprint,
        "estancadas": estancadas,
        "no_qa_inst": no_qa_inst,
        "my_rounds": my_rounds,
        "hallazgos": hallazgos,
        "tracked_qa_h": round(tracked_qa_h, 1),
    }


def build_qa_report(d: dict, today_str: str) -> str:
    R = []
    me = d["me"]
    cola = len(d["queue"])
    cuello = cola > QA_QUEUE_MAX
    R.append(f"# Mi Diagnóstico QA — {me['name']} — {today_str}")
    R.append(f"## Cola de QA en el sprint activo · Proyectos: {', '.join(d['proys']) or '—'}")
    R.append("")
    R.append("---")
    R.append("")

    if cola == 0:
        R.append("> No tienes tareas en estado `qa` asignadas a ti en el sprint activo "
                 f"(hay {d['qa_total_sprint']} en QA en total). Si esperabas ver tu cola, "
                 "revisa que las tareas en QA te tengan como **assignee**.")
        R.append("")
        R.append("---")
        R.append("")

    # Resumen
    R.append("### Mi tablero QA")
    R.append("")
    R.append("```")
    R.append(f"{'Indicador':<30} {'Tú':>8}  {'Meta':<10} {'Estado'}")
    R.append("─" * 60)
    R.append(f"{'Cola de QA (tareas a revisar)':<30} {cola:>8}  {'≤5':<10} {_mark(not cuello)}")
    R.append(f"{'Estancadas en QA (>3d)':<30} {len(d['estancadas']):>8}  {'0':<10} {_mark(not d['estancadas'])}")
    R.append(f"{'En cola sin QA Instructions':<30} {len(d['no_qa_inst']):>8}  {'0':<10} {_mark(not d['no_qa_inst'])}")
    R.append(f"{'Hallazgos (rounds) generados':<30} {d['my_rounds']:>8}  {'—':<10} ℹ️")
    R.append(f"{'Tiempo registrado en QA':<30} {d['tracked_qa_h']:>7.1f}h  {'—':<10} ℹ️")
    R.append("```")
    R.append("")
    R.append("---")
    R.append("")

    # Detalle cola
    R.append("### Mi cola de QA" + (" ⚠️ cuello de botella" if cuello else ""))
    R.append("")
    if d["queue"]:
        R.append("```")
        hh = f"{'ID':<11} {'Tarea':<34} {'Proy':<8} {'Días en QA':>11} {'QA Inst.':>9}"
        R.append(hh)
        R.append("─" * len(hh))
        for q in d["queue"]:
            R.append(f"{q['id']:<11} {_trunc(q['name']):<34} {q['proj']:<8} {q['days']:>10.1f}d {('sí' if q['has_qa'] else 'NO'):>9}")
        R.append("```")
    else:
        R.append("Cola vacía. ✅")
    R.append("")

    # Sin QA Instructions (no testeable)
    R.append("### En cola sin QA Instructions (no se pueden testear bien)")
    R.append("")
    if d["no_qa_inst"]:
        R.append("Estas tareas están en tu cola pero no traen QA Instructions: conviene devolverlas al dev/Owner antes de revisar.")
        R.append("")
        R.append("```")
        hh = f"{'ID':<11} {'Tarea':<34} {'Proy':<8}"
        R.append(hh)
        R.append("─" * len(hh))
        for a in d["no_qa_inst"]:
            R.append(f"{a['id']:<11} {_trunc(a['name']):<34} {a['proj']:<8}")
        R.append("```")
    else:
        R.append("Todas tus tareas en cola tienen QA Instructions. ✅")
    R.append("")

    R.append(f"### Hallazgos generados en el sprint ({d['my_rounds']})")
    R.append("")
    R.append("Solo cuentan los rounds cuyo comentario tuyo cae dentro de la ventana del sprint.")
    R.append("")
    if d["hallazgos"]:
        R.append("```")
        hh = f"{'Tarea':<30} {'Proy':<8} {'Devuelta a':<13} {'Fecha':<6} {'Hallazgo (tu comentario)'}"
        R.append(hh)
        R.append("─" * len(hh))
        for h in d["hallazgos"]:
            R.append(f"{_trunc(h['name'], 30):<30} {h['proj']:<8} {h['returned_to']:<13} {h['date'][5:]:<6} {h['comment'][:55]}")
        R.append("```")
    else:
        R.append("Sin hallazgos atribuidos a ti dentro de la ventana del sprint.")
    R.append("")
    R.append(f"**Tiempo registrado en QA:** {d['tracked_qa_h']}h en tareas de 'Revisión integral de QA'.")
    R.append("")
    R.append("---")
    R.append("")
    R.append("### Coaching — qué ajustar hoy")
    R.append("")
    R.append("<!-- AI:COACHING -->")
    R.append("")
    R.append("---")
    R.append(f"*Generado el {today_str} por Dev-Check (vista QA). Corre con `--analyze` para el coaching AI.*")
    return "\n".join(R)


def build_qa_context(report_text: str, d: dict, today: str) -> str:
    subset = {
        "me": d["me"]["name"], "rol": "QA",
        "cola_qa": len(d["queue"]),
        "estancadas_qa": len(d["estancadas"]),
        "sin_qa_instructions": len(d["no_qa_inst"]),
        "hallazgos_rounds": d["my_rounds"],
        "tiempo_qa_h": d["tracked_qa_h"],
        "proys": d["proys"],
    }
    return "\n".join([
        f"# Contexto de Coaching de QA — {d['me']['name']} — {today}",
        "",
        "## INSTRUCCIONES PARA EL MODELO",
        "Eres un coach de QA. Lee los datos del tester y responde SOLO en español.",
        "DEBES devolver EXACTAMENTE este formato:",
        "```",
        "[COACHING]",
        "1. acción concreta y priorizada",
        "2. acción concreta y priorizada",
        "3. acción concreta y priorizada",
        "[/COACHING]",
        "```",
        "Da entre 3 y 5 acciones enfocadas en QA (cola, estancadas, tareas sin QA Instructions). NO alteres datos numéricos.",
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

    # Vista por rol: QA usa su cola (assignee); dev usa Owner.
    if me["id"] in QA_IDS:
        d = analyze_qa(now, headers, config, me)
        report_text = build_qa_report(d, today_str)
        make_ctx = build_qa_context
    else:
        d = analyze_me(now, headers, config, me)
        report_text = build_report(d, today_str)
        make_ctx = build_analysis_context

    report_path = ROOT / REPORT_NAME.format(date=today_str)
    report_path.write_text(report_text, encoding="utf-8")
    (ROOT / DATA_NAME.format(date=today_str)).write_text(
        json.dumps(d, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Reporte generado: {report_path.name}")

    if do_analyze:
        ctx_text = make_ctx(report_text, d, today_str)
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
