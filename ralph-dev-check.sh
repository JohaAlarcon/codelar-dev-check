#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# ralph-dev-check.sh — Orquestador de la herramienta de autoayuda del desarrollador
# Auto-detecta tu perfil (tu token de ClickUp) y analiza SOLO tus tareas.
# Por defecto corre rápido y SIN AI. Pasa --analyze para el coaching AI (más lento).
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEV_SCRIPT="${SCRIPT_DIR}/dev_check.py"
TODAY="$(date -u +%Y-%m-%d)"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}$*${NC}"; }
warn()  { echo -e "${YELLOW}$*${NC}"; }
error() { echo -e "${RED}$*${NC}"; }
die()   { error "$*"; exit 1; }

usage() {
    cat <<EOF
Uso: ./ralph-dev-check.sh [opciones]

Opciones:
  (sin args)   Diagnóstico determinístico rápido (SIN AI) — recomendado para el día a día
  --analyze    Agrega coaching AI personalizado (usa gemini/claude, más lento)
  --dev NOMBRE Modo preview (manager): analiza el perfil de otro dev en vez del tuyo
  --setup      Descubre y cachea los sprints activos (config.json)
  -h, --help   Muestra esta ayuda

Primera vez:
  cp .env.example .env     # y pon tu token personal de ClickUp en CLICKUP_API_KEY
  ./ralph-dev-check.sh --setup
  ./ralph-dev-check.sh

Ejemplos:
  ./ralph-dev-check.sh                      # tu diagnóstico (auto-detecta tu perfil)
  ./ralph-dev-check.sh --analyze            # + coaching AI
  ./ralph-dev-check.sh --dev "Damian L."    # preview de otro dev (solo manager)
EOF
}

find_python() {
    local -a candidates=(python3.13 python3.12 python3.11 python3.10 python3 python)
    for c in "${candidates[@]}"; do
        if command -v "${c}" &>/dev/null; then
            local ver; ver="$("${c}" -c 'import sys; print(sys.version_info >= (3,10))' 2>/dev/null)"
            if [[ "${ver}" == "True" ]]; then command -v "${c}"; return 0; fi
        fi
    done
    return 1
}

run_check() {
    local extra_args=("$@")
    echo -e "${CYAN}─────────────────────────────────────────────${NC}"
    echo -e "${CYAN}  Dev-Check — ${TODAY}${NC}"
    echo -e "${CYAN}─────────────────────────────────────────────${NC}"

    local PYTHON_CMD
    PYTHON_CMD="$(find_python)" || die "Python 3.10+ no encontrado. Instalar: brew install python@3.13 (o python.org)"

    [[ -f "${DEV_SCRIPT}" ]] || die "No se encontró ${DEV_SCRIPT}"

    if [[ -f "${SCRIPT_DIR}/.env" ]]; then
        # shellcheck disable=SC1091
        set -a; source "${SCRIPT_DIR}/.env" 2>/dev/null || true; set +a
    fi
    [[ -n "${CLICKUP_API_KEY:-}" ]] || die "CLICKUP_API_KEY no configurada. Copia .env.example a .env y pon tu token."

    if ! ${PYTHON_CMD} "${DEV_SCRIPT}" "${extra_args[@]}" 2>&1 | grep -v '\[ERROR\] GET.*time_in_status'; then
        die "Fallo al generar tu diagnóstico."
    fi

    local report="${SCRIPT_DIR}/Mi_Reporte_${TODAY}.md"
    if [[ -f "${report}" ]]; then
        echo ""
        info "Listo. Tu reporte: ${report}"
        echo ""
        cat "${report}"
    fi
}

main() {
    case "${1:-}" in
        -h|--help)  usage ;;
        --setup)
            local PYTHON_CMD; PYTHON_CMD="$(find_python)" || die "Python 3.10+ no encontrado"
            if [[ -f "${SCRIPT_DIR}/.env" ]]; then set -a; source "${SCRIPT_DIR}/.env" 2>/dev/null || true; set +a; fi
            [[ -n "${CLICKUP_API_KEY:-}" ]] || die "CLICKUP_API_KEY no configurada."
            ${PYTHON_CMD} "${DEV_SCRIPT}" --setup
            ;;
        --run)      shift; run_check "$@" ;;
        *)          run_check "$@" ;;   # reenvía --analyze / --dev "Nombre" al script
    esac
}

main "$@"
