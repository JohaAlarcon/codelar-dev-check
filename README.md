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

### Cómo saber tu ID de ClickUp

**Normalmente NO lo necesitas**: la herramienta te detecta sola a partir de tu token
(verás `Perfil detectado: TuNombre (id 67211378)` al correrla). Ese número entre paréntesis **es tu ID**.

Si quieres obtenerlo aparte (por ejemplo para verificar que te detecta bien), corre:

```bash
curl -s -H "Authorization: $CLICKUP_API_KEY" https://api.clickup.com/api/v2/user
```

En la respuesta, el campo `"id"` dentro de `"user"` es tu ID de desarrollador.

## Uso diario

```bash
./ralph-dev-check.sh            # diagnóstico rápido (recomendado) — te detecta por tu token
./ralph-dev-check.sh --analyze  # + coaching AI personalizado (necesita gemini o claude CLI)
```

El reporte se imprime en pantalla y se guarda como `Mi_Reporte_YYYY-MM-DD.md`.

### Usar tu ID explícitamente (opcional)

Si la auto-detección no te identifica bien (p. ej. tu usuario de ClickUp no coincide con tu nombre),
puedes pasar tu ID o tu nombre directamente con `--dev`:

```bash
./ralph-dev-check.sh --dev 67211378       # con tu ID
./ralph-dev-check.sh --dev "Damian L."    # o con tu nombre
```

> Nota: `--dev` analiza el perfil indicado en vez del tuyo. Sirve para verificar tu propio ID o,
> si eres líder/manager, para previsualizar el reporte de otra persona del equipo.

## Qué te muestra

| Dimensión | Meta del lineamiento |
|-----------|----------------------|
| Documentación QA/Done (QA Instructions ≥50 chars + PR Link + Deploy Instructions) | ≥80% |
| Plazos (tus tareas vencidas) | ≤15% |
| Tracking (horas gastadas / estimadas) | 70–120% |
| Calidad (rounds de QA promedio) | ≤1.0 |
| Estimación (tareas sin Sprint Points) | 0 |
| Tiempo en estado, **solo los estados que controlas** (TO DO/IN PROGRESS ≤10d · Merge→Dev ≤3d) | 0 estancadas |
| **Capacidad** (horas pendientes vs. disponibles en el sprint) | carga ≤ disponible |
| Saturación (>15 tareas · 3+ proyectos) | sin señales |

**Capacidad** se calcula igual que el guardián: `días hábiles restantes × 8h − reuniones prorrateadas`,
donde los días hábiles son lunes-viernes **menos los festivos colombianos 2026**. Tu carga pendiente es la
suma de `estimado − gastado` de tus tareas no-terminadas. Te dice cuántas horas te quedan disponibles vs.
cuánto trabajo tienes por delante.

Cada dimensión sale con ✅ / ⚠️ y el detalle de las tareas que debes revisar.

### Vista QA (perfiles de QA)

Si el token es de un **QA tester** (Juan M. o José F.), la herramienta cambia a una vista por **cola de
QA** (tareas en estado `qa` asignadas a ti, no por Owner) y muestra métricas propias de QA:

| Indicador QA | Meta |
|--------------|------|
| Cola de QA (tareas a revisar) | ≤5 (si no, cuello de botella) |
| Estancadas en QA (>3d esperando revisión) | 0 |
| En cola **sin QA Instructions** (no se pueden testear bien → devolver al dev) | 0 |
| Hallazgos (rounds) generados — **acotados al sprint**, con detalle de a quién se devolvió cada tarea | informativo |
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
