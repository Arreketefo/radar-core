# radar-core — Núcleo común de la familia de radares

> Documento de arquitectura e implementación. Prosa en español, código e identificadores en inglés.
> Paquete Python: `radar_core`. Repo propio y versionado con SemVer.
> Versión del doc: v1.2 (2026-08-03).
> Consumidores: **Vigía**, **Ojeador**, **Tasador** y **Audímetro**.

---

## 1. Justificación: la regla de tres, cumplida

La decisión original (2026-07-06, al definir Ojeador) fue explícita: dos apps se
copian, la abstracción común solo se extrae cuando exista un TERCER radar — porque
solo entonces se sabe qué es genérico *de verdad* y qué era coincidencia. Con Tasador
definido, la regla se cumple: los tres comparten literalmente los mismos módulos de
infraestructura (ya endurecidos por reviews y producción en Vigía) y los tres
necesitan el mismo control por Telegram (spec en radar-ops §7).

**Qué gana la extracción:** un bugfix de infraestructura se arregla una vez (no tres);
botcontrol se implementa una vez; los radares nuevos arrancan en horas.
**Qué NO debe pasar:** que el core absorba dominio. La línea divisoria es el criterio
de admisión más importante de este documento (§3).

---

## 2. Contenido del paquete

Todo procede de módulos de Vigía ya probados (tests incluidos, se mueven con el código):

```
radar_core/
├── ratelimit.py      TokenBucket (sleep FUERA del lock)
├── http.py           get_json/post_json/request_json: backoff exponencial + jitter,
│                     CircuitBreaker/CircuitOpenError. Regla: secretos SIEMPRE en
│                     headers, jamás en URLs (los logs los imprimen)
├── stats.py          robust_baseline (mediana+MAD con MAD=0 manejado),
│                     drop_display (sin cero negativo)
├── store.py          open_store (aiosqlite, WAL, foreign_keys), TIMESTAMP_FMT,
│                     utcnow_str, _iso, tabla meta (get/set), prune por edad,
│                     patrón scan_state POR INTENTO (anti-starvation),
│                     tabla config_overrides (para botcontrol)
├── config.py         RadarSettings base: env_ignore_empty=True, prefijo por app,
│                     helpers de parseo (listas por comas, fechas), validadores
├── runtime.py        daemon base: APScheduler + tick_lock + wait_idle() en shutdown
│                     (SIGTERM espera al tick en vuelo) + aclose() duck-typed de
│                     componentes + healthcheck por meta.last_tick_at
├── notifiers/
│   ├── telegram.py   transporte (sendMessage, Markdown escape, raise_for_status,
│   │                 aclose) — el RENDERizado del mensaje es de dominio: el
│   │                 notifier recibe un `render(item) -> str` o texto ya hecho
│   ├── email.py      SMTP con STARTTLS condicional (has_extn)
│   └── console.py    fallback a log
├── botcontrol.py     control por Telegram (spec radar-ops §7): long-polling
│                     getUpdates con offset persistido en meta, allowlist de
│                     chat_ids, comandos comunes (/status /config /set /reset
│                     /pausa /reanuda /ayuda /digest), mecanismo config_overrides
│                     (whitelist + validadores por clave, merge sobre Settings en
│                     cada tick), registro de comandos de dominio vía hooks
└── cli.py            helpers de subcomandos (add/list/rm patterns)
```

Decisiones incluidas "de serie" (lecciones de Vigía, no negociables en el core):
- verificar formas de API en vivo antes de fiarse de docs (3 APIs muertas halladas);
- ledgers de dedup en UNA sola unidad de precio;
- baselines nunca mostrados con muestra < mínimo;
- control de riada (cap de alertas por unidad de trabajo y tick);
- `.dockerignore` y plantillas Docker/Makefile de referencia (directorio `templates/`).

## 3. Criterio de admisión al core (la línea)

**Entra**: código que los TRES radares usan sin conocer el dominio (transporte,
persistencia genérica, estadística robusta, daemon, bot). Test: ¿compila sin
importar nada de vuelos/artículos/pisos? 

**NO entra jamás**: contracts de dominio (FlightQuote/Listing/PropertyListing),
schedulers de dominio (round-robin rutas×mes ≠ watch×source+RSS ≠ IMAP push),
match/deal engines, fuentes concretas, parsers, renderers de mensaje (qué dice una
alerta es dominio; cómo se envía es core).

Regla operativa: si una PR al core menciona "vuelo", "hotel", "listing", "piso" o
"zona", está mal dirigida.

---

## 4. Distribución y versionado

- **Fuente versionada** (`github.com/Arreketefo/radar-core`): los tags SemVer son
  la referencia estable para consumidores y revisiones. Un consumidor público
  puede depender de un tag Git; el desarrollo conjunto puede usar un path local.
- **Vendoring para despliegues sin credenciales**: las instalaciones de producción
  pueden copiar un snapshot revisado dentro del contexto Docker. Cada app declara
  entonces la dependencia por path local dentro de su propio árbol —
  ```toml
  dependencies = ["radar-core"]
  [tool.uv.sources]
  radar-core = { path = "vendor/radar-core" }
  ```
  — y un target `make vendor-core` refresca la copia
  (`rsync -a --delete --exclude .git ../radar-core/ vendor/radar-core/`),
  como prerrequisito de `install`, `build` y `deploy`. Reglas:
  - `vendor/` va en el `.gitignore` de cada app para evitar duplicar historial y
    NO se excluye en `.dockerignore` (el build lo necesita).
  - El Dockerfile copia `vendor/` junto a `pyproject.toml`/`uv.lock` ANTES del
    primer `uv sync` (la dependencia por path debe existir en la capa de deps).
  - Los repositorios públicos deben ofrecer además una ruta de instalación
    reproducible; no pueden depender únicamente de un directorio ignorado.
- **Versionado**: tags SemVer en el repo del core como puntos de referencia;
  la versión efectiva de cada app es el snapshot vendorizado (registrado por
  el commit del core anotado en el mensaje de deploy). CHANGELOG.md obligatorio.
- Tests propios del core + target `make test-consumers` que ejecuta las suites
  de las apps locales contra el core en desarrollo antes de taggear.

---

## 5. Plan de adaptación por app

### 5.1 Vigía (construida y en producción) — migración COMPLETA
> Decisión del propietario (2026-07-06): refactorización íntegra en un solo
> esfuerzo planificado — al terminar, Vigía NO conserva ninguna copia local de
> código común: todo lo de §2 se importa de `radar_core`.

1. Crear el core moviendo los módulos §2 desde Vigía, con tests propios.
2. Refactorizar Vigía entera en la misma tanda:
   - eliminar `vigia/ratelimit.py` y `vigia/http.py` → imports del core;
   - `engine.py` conserva SOLO dominio (is_deal, dedup_key) y re-exporta
     `robust_baseline` del core (los consumidores internos no cambian);
   - notifiers: el transporte (Telegram/SMTP) pasa al core; el render
     (`deal_lines`, mensaje de Telegram con ciudades) se queda en Vigía;
   - `store.py`: `PriceStore` extiende la base del core (conexión/WAL, meta,
     prune, timestamps); las queries de dominio se quedan;
   - `config.py` usa la base del core; `__main__`/healthcheck usan el runtime
     del core; fuentes actualizan imports.
3. **Red de seguridad obligatoria** (lo que sustituye a la gradualidad):
   suite completa de Vigía en verde sin tocar sus asserts + ruff/mypy en ambos
   proyectos + build Docker con vendoring + tick real de humo + pasada de
   review del diff + redeploy. El comportamiento observable no cambia.
4. **botcontrol**: sigue siendo fase posterior (radar-ops fase 2): se
   implementa en el core y Vigía lo adopta registrando sus comandos de dominio.

### 5.2 Ojeador (definida, no construida) — nace sobre el core
- Su doc §12 pasa de "copiar de Vigía" a "depender de radar-core" (actualizado).
- Implementación v1 = `uv add radar-core` + solo dominio: contracts, MatchEngine,
  fuentes (eBay/Chollometro/Wallapop), scheduler de dos loops, CLI de watches.
- Estimación honesta: el core le ahorra ~40% del trabajo de v1 y todo el botcontrol.

### 5.3 Tasador (definida) — nace sobre el core
- Igual que Ojeador: core + dominio (zonas, parsers IMAP, clusters, tasación).
- El módulo IMAP NO va al core (solo Tasador lo usa; si un cuarto radar lo
  necesitara, se promociona entonces — la misma regla de tres, recursiva).

### 5.4 Orden de ejecución
```
fase 0: repo radar-core (privado) + módulos + tests       (medio día)
fase 0b: refactorización COMPLETA de Vigía sobre el core   (misma tanda, §5.1)
fase 1: botcontrol en el core (spec radar-ops §7)          (1 día)
fase 2: Ojeador v1 sobre el core                           (su roadmap)
fase 3: Tasador v1 sobre el core                           (su roadmap)
```

---

## 6. Riesgos y mitigaciones

| Riesgo | Impacto | Mitigación |
|---|---|---|
| El core acumula dominio ("solo este if") | Acoplamiento de los 3 radares | Criterio §3 + regla operativa de palabras prohibidas |
| Cambio de core rompe un consumidor | Radar caído | Tags SemVer + snapshots vendorizados + make test-consumers antes de taggear |
| La migración completa de Vigía introduce regresiones | Radar en producción degradado | Red de seguridad §5.1.3: suite intacta en verde, lint/tipos, build vendorizado, tick real, review del diff y redeploy verificado |
| El vendoring se desincroniza del repo core | Apps con core viejo | `vendor-core` como prerrequisito de install/build/deploy; el snapshot se refresca solo en cada operación |
