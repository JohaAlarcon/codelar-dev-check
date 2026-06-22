# Dev-Check — Autoayuda para desarrolladores CODELAR

Herramienta personal para que **cada desarrollador** revise, en segundos, qué tan
alineado está su trabajo con el lineamiento del equipo: documentación, plazos,
estimación, calidad (rounds de QA), tareas estancadas y saturación.

- Corre con **tu propio token** de ClickUp y analiza **solo tus tareas** (auto-detecta tu perfil).
- Por defecto es **determinística y rápida** — sin AI, sin esperas, resultado consistente.
- Con `--analyze` agrega un coaching AI personalizado ("qué ajustar hoy"). Es opcional y más lento.

## Instalación (una sola vez)

Necesitas **Python 3.10+** (`python3 --version`). Si no lo tienes: [python.org](https://www.python.org/downloads/) o `brew install python@3.13`.

```bash
git clone <url-de-este-repo>
cd codelar-dev-check
cp .env.example .env
# Abre .env y pega tu token de ClickUp en CLICKUP_API_KEY
./ralph-dev-check.sh --setup
```

### Cómo obtener tu token de ClickUp
ClickUp → tu avatar (abajo-izquierda) → **Settings** → **Apps** → **API Token** → **Generate**.
Cópialo (empieza con `pk_`) y pégalo en tu archivo `.env`. **Nunca lo compartas ni lo subas a git** (`.env` ya está en `.gitignore`).

## Uso diario

```bash
./ralph-dev-check.sh            # diagnóstico rápido (recomendado)
./ralph-dev-check.sh --analyze  # + coaching AI personalizado (necesita gemini o claude CLI)
```

El reporte se imprime en pantalla y se guarda como `Mi_Reporte_YYYY-MM-DD.md`.

## Qué te muestra

| Dimensión | Meta del lineamiento |
|-----------|----------------------|
| Documentación QA/Done (QA Instructions ≥50 chars + PR Link + Deploy Instructions) | ≥80% |
| Plazos (tus tareas vencidas) | ≤15% |
| Tracking (horas gastadas / estimadas) | 70–120% |
| Calidad (rounds de QA promedio) | ≤1.0 |
| Estimación (tareas sin Sprint Points) | 0 |
| Tiempo en estado, **solo los estados que controlas** (TO DO/IN PROGRESS ≤10d · Merge→Dev ≤3d) | 0 estancadas |
| Saturación (>15 tareas · >80h · 3+ proyectos) | sin señales |

Cada dimensión sale con ✅ / ⚠️ y el detalle de las tareas que debes revisar.

### Vista QA (perfiles de QA)

Si el token es de un **QA tester** (Juan M. o José F.), la herramienta cambia a una vista por **cola de
QA** (tareas en estado `qa` asignadas a ti, no por Owner) y muestra métricas propias de QA:

| Indicador QA | Meta |
|--------------|------|
| Cola de QA (tareas a revisar) | ≤5 (si no, cuello de botella) |
| Estancadas en QA (>3d esperando revisión) | 0 |
| En cola **sin QA Instructions** (no se pueden testear bien → devolver al dev) | 0 |
| Hallazgos (rounds) generados | informativo |
| Tiempo registrado en "Revisión integral de QA" | informativo |

El reporte de cada rol se mide solo en lo que esa persona controla: el dev no ve como "estancadas" las
tareas que ya pasaron a QA, y el QA ve su cola en lugar de tareas por Owner.

## Privacidad

La herramienta es **solo lectura** y solo ve lo que tu token puede ver. No modifica
ninguna tarea ni envía tus datos a ningún servidor: el coaching AI (opcional) usa el
CLI de AI que tengas instalado localmente.

## Para el coaching AI (opcional)

`--analyze` usa el CLI de `gemini` o `claude` si está instalado en tu `PATH`. Si no
tienes ninguno, la herramienta lo avisa y simplemente omite esa sección — el resto del
reporte funciona igual.
