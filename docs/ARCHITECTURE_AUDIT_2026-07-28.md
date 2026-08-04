# Auditoría de arquitectura — Hades + Hades Research Lab

**Fecha:** 2026-07-28 · **Rama:** `claude/hades-bot-wiring-issues-8bcbnj` · **HEAD:** `6e50cb8` · **Versión:** 0.10.0
**Alcance:** `hades.md`, `docs/`, `backend/` completo (417 archivos), `frontend/`, y el repo independiente
`Projects/HadesResearchLab` (161 archivos).
**Método:** lectura de código y trazado de suscripciones evento por evento. **No había stack levantado**
durante esta pasada; todo hallazgo se cita con `archivo:línea` y es verificable en frío. Donde una
afirmación depende de runtime, se marca explícitamente como **no verificado en vivo**.

**Este documento no implementa nada.** Es el insumo para decidir el plan de evolución.

---

## 0. Resumen ejecutivo

Hades es, en calidad de construcción, una plataforma seria: DDD honesto, contextos con fronteras reales,
invariantes de seguridad de capital verificados por test, y un gate de calidad (ruff · mypy --strict ·
550 tests) que casi ningún proyecto de este tamaño sostiene. **El problema de Hades no es la calidad del
código: es que el grafo de eventos tiene menos aristas de las que la documentación afirma.**

La auditoría encontró **tres circuitos abiertos** que, juntos, explican por qué la plataforma no aprende
y por qué no opera de forma sostenida. Ninguno es un bug: son piezas correctas que nunca se conectaron.

| # | Circuito abierto | Consecuencia | Evidencia |
|---|---|---|---|
| **A1** | `KnowledgeFeedback.record_outcome()` **no tiene ni un solo llamador** | El ledger de outcomes solo recibe negativos débiles de `TokenRejected`. El dataset es de **una sola clase**. | `contexts/learning/application/knowledge_feedback.py:54` — cero referencias externas |
| **A2** | El **Strategy Engine no tiene consumidor**. `EnsembleSignalGenerated` no lo escucha nadie. | 15 estrategias, DynamicWeightEngine, SelfEvaluator y ShadowLifecycle son un cómputo que termina en el vacío. | `grep EnsembleSignalGenerated` → solo productor + registry |
| **A3** | Una promoción de modelo **no surte efecto hasta reiniciar el worker**. | `set_active()` solo se llama en el arranque; nadie escucha `ModelPromoted`. | `ops/committee_runtime.py:177` vs. `:170` |

A esto se suman **dos contextos completamente muertos** (`scoring`, `wallet`), un **proceso vacío en la
topología** (`engine`), y un **puente con el Research Lab que es estructuralmente imposible de cerrar
tal como están definidos hoy los dos lados** (§7).

**La conclusión de fondo:** Hades no tiene un problema de umbrales ni de calibración. Tiene un problema
de *cierre de lazo*. Mientras A1 siga abierto, ningún ajuste de configuración puede producir aprendizaje,
porque el aprendizaje no tiene de dónde aprender.

---

## 1. Estado actual

### 1.1 Lo que existe y corre

| Capa | Estado | Dónde |
|---|---|---|
| Shared kernel (VOs, eventos, CQRS, config, persistencia, observabilidad) | Completo y sano | `shared_kernel/` |
| Scanner (6 fuentes DEX, RPC manager con failover, pipeline con backpressure) | Corre; 2 de 6 fuentes en 404 documentado | `contexts/scanner/` |
| Feature Engine (~100 features, 6 extractores) | Corre | `contexts/features/` |
| Security Engine (10 analizadores, veto por flag crítico) | Corre | `contexts/security/` |
| Wallet Intelligence | Corre | `contexts/intelligence/` |
| AI Committee (12 especialistas + meta-model) | Corre, **siempre con los priors por defecto** | `contexts/learning/` |
| Strategy Engine (15 estrategias) | Corre, **su salida no la consume nadie** | `contexts/strategy/` |
| Risk Manager + Portfolio | Corre; único aprobador; estado persistido | `contexts/risk/`, `contexts/portfolio/` |
| Execution Engine + PositionMonitor + price oracle | Corre en paper | `contexts/execution/`, `contexts/market/` |
| Research (interno al Core) | Corre si `RESEARCH_LAB_ENABLED` + `RESEARCH_AUTO_RESEARCH` | `contexts/research/` |
| Audit / Monitoring / Notification | Corre | varios |
| **`scoring`** | **Muerto — solo dominio, cero referencias externas** | `contexts/scoring/` |
| **`wallet`** | **Muerto — solo dominio, cero referencias externas** | `contexts/wallet/` |

### 1.2 Topología de procesos

Siete servicios (`api`, `engine`, `worker`, `scheduler`, `notification`, `watchdog`, `dashboard`), pero
**la totalidad del pipeline de negocio vive en un solo proceso: `worker`** (`ops/worker.py:50-146`).
El proceso `engine` es explícitamente un placeholder supervisado que no corre lógica alguna
(`ops/engine.py:26-43`, honesto en su docstring).

Consecuencia: Scanner, Security, Intelligence, Committee, Strategy, Risk, Execution, Research, Audit y
Performance comparten **un único event loop de asyncio y un único proceso Python**. No hay paralelismo
real posible: un backtest CPU-bound del Research Lab bloquea el descubrimiento de tokens.

---

## 2. Fortalezas

Estas no son cortesías; son propiedades que costaría mucho recuperar si se pierden en la evolución y
que el plan de fases debe tratar como **restricciones, no como material reciclable**.

1. **Invariantes de money-safety verificados por test, no por convención.**
   `TradeApproved` se construye en exactamente un sitio (`contexts/risk/application/manager.py:305`).
   El aislamiento del Research Lab lo verifica un test AST que rompe el build. Live es imposible por
   ausencia de adaptadores, no por un `if`.

2. **Fail-closed consistente y bien razonado.** Risk envuelve `_decide()` y convierte cualquier
   excepción en REJECT. El parser de bundles (`candidates.py`) rechaza a la primera cosa que no puede
   avalar. El chequeo `live_executor` falla en cerrado. Este patrón está aplicado con criterio, no
   copiado mecánicamente.

3. **Explicabilidad como restricción estructural, no como feature.** El registry almacena conjuntos de
   pesos transparentes (`sigmoid(bias + Σwᵢxᵢ)`), y el contrato de importación **rechaza pickles por
   diseño** (`candidates.py:9-17`). Es la decisión más valiosa del proyecto y la que más restringe la
   evolución (§7).

4. **Contextos con fronteras reales.** Los contextos no se importan entre sí; se comunican por eventos.
   Las excepciones son la capa `ops/` (composition root por proceso), que es exactamente donde deben
   estar.

5. **Configuración por secciones tipada** (`shared_kernel/config/settings.py`, 30 secciones pydantic) con
   snapshots auditables.

6. **Persistencia con adaptadores duales** (`Postgres*` / `InMemory*`) elegidos por presencia de
   `container.database`. Permite testear todo el grafo sin stack.

---

## 3. Debilidades

### D1 — El lazo de aprendizaje está abierto en su articulación principal *(crítico)*

`KnowledgeFeedback` (`contexts/learning/application/knowledge_feedback.py`) documenta dos entradas:

- `on_token_rejected` — **suscrita** a `TokenRejected` (`:52`).
- `record_outcome` — las etiquetas reales de un trade cerrado. Su docstring dice
  *"called by the execution/portfolio context in a later phase"* (`:10-11`). **Esa fase nunca llegó.**

`grep -rn record_outcome` sobre todo el backend devuelve, para el contexto `learning`, únicamente la
definición. Nadie suscribe `PositionClosed` en nombre de `learning`: los cuatro suscriptores de
`PositionClosed` son `position_monitor`, `portfolio_manager`, `strategy/subscriber` y `risk_runtime`.

**Efecto medible:** el `OutcomeStore` solo contiene muestras con `was_rejected=True`,
`label_roi_positive=False`, `label_hit_tp=False`, `label_hit_sl=True`, `weight=0.5` (`:95-104`). Es un
dataset **de una única clase**. Los "230 outcomes etiquetados" reportados en producción son 230 negativos.

**Efecto en cascada:** `ValidationConfig.min_auc = 0.55` (`validation.py:52`). El AUC sobre una sola
clase es indefinido/0.5. Ningún candidato puede pasar la validación **jamás**, con independencia de
cuántos datos se acumulen. Por tanto `propose_promotion` nunca se llama, el registry nunca tiene cards
activas, y `_load_active_committee()` (`ops/committee_runtime.py:170`) nunca encuentra nada. El comité
queda permanentemente en `default_committee()`.

> Corrección a un diagnóstico anterior registrado en Notion: el "cerrojo de arranque en frío" se atribuyó
> a que `P(ROI+)` arranca en 0.4626 contra un mínimo de 0.55. Eso es un síntoma. **La causa es que no
> existe ruta de escritura para las etiquetas positivas.** Bajar el umbral no arreglaría nada: produciría
> entradas, pero sus resultados seguirían sin llegar al ledger, y el comité seguiría sin aprender.

### D2 — El Strategy Engine es un cómputo sin destinatario *(alto)*

`EnsembleSignalGenerated` se publica en `contexts/strategy/application/engine.py:159` y **no tiene ningún
suscriptor en todo el repositorio**. Las únicas otras apariciones son el registro en `bootstrap.py` y
docstrings.

El Risk Manager se suscribe directamente a `CommitteePredictionGenerated`
(`ops/risk_runtime.py:137` → `RiskHandler`). El camino real es:

```
committee ──CommitteePredictionGenerated──► risk
strategy  ──EnsembleSignalGenerated───────► (nadie)
```

Se está pagando el coste completo de la Fase 10 —15 estrategias, ensemble ponderado, motor de pesos
dinámicos, autoevaluación, ciclo de vida shadow— por cero influencia en las decisiones. La bandera
`strategy.gate_risk` (`settings.py:676`) que supuestamente habilitaría este camino **no se lee en ninguna
parte fuera de logs y snapshots** (`ops/strategy_runtime.py:83,104`): es decorativa.

Además, las señales `SELL`/`EXIT` que el Strategy Engine puede emitir mueren aquí. Hoy las salidas las
produce exclusivamente el `PositionMonitor` a partir del sobre TP/SL que aprobó Risk en la entrada.

### D3 — Una promoción de modelo no llega a producción sin reinicio *(alto)*

`CommitteeManager.set_active()` se invoca en un solo lugar: `_load_active_committee()`, llamado desde
`start()` (`ops/committee_runtime.py:265`). No hay suscripción a `ModelPromoted`. Por tanto todo el
aparato de promoción human-gated —que es correcto y está bien construido— desemboca en un registry cuyo
cambio **el runtime no observa**. Un operador que promociona un modelo ve el evento en el audit trail y
ningún cambio de comportamiento.

### D4 — Dos contextos completamente muertos, uno de ellos peligroso *(medio)*

- **`contexts/scoring`**: cero referencias externas. Su `FinalScoreComputed` aparece en el diagrama de
  `docs/architecture.md:21-22` como una etapa del pipeline. **Esa etapa no existe.** Sus variables
  `SCORING_*` ya se retiraron del dashboard (`6008192`), pero el contexto sigue en el árbol de fuentes
  induciendo a error a cualquiera que lea la arquitectura.
- **`contexts/wallet`**: solo `domain/`. `WalletScoreComputed` nunca se publica. El diagrama lo lista
  como productor (`architecture.md:122`).

### D5 — El event store es in-memory por construcción *(medio, ya conocido como H1)*

`bootstrap.py:314`: `event_store: EventStore = InMemoryEventStore()  # Postgres-backed store: later phase`.
Es incondicional — no hay rama Postgres. Consecuencia observada en producción y registrada en Notion:
durante el incidente de disco, Risk aprobó 12 operaciones, ninguna se ejecutó, y **la causa fue
indeterminable porque el event store estaba vacío y los logs habían rotado**. Sin trazabilidad durable,
cada incidente cuesta una sesión de arqueología.

### D6 — Todo el pipeline en un event loop *(medio)*

Ver §1.2. Agravado por el hecho de que el Research Lab interno (`contexts/research`) corre **dentro del
mismo worker** y sus motores de backtest/Monte Carlo son CPU-bound. Con `RESEARCH_AUTO_RESEARCH=true` en
una caja de 2 vCPU, un estudio compite directamente con el descubrimiento de tokens.

### D7 — Duplicación de la capacidad de investigación en dos stacks *(medio-alto, estratégico)*

Existen **dos Research Labs**:

| | `Hades/backend/contexts/research` | `HadesResearchLab` (repo aparte) |
|---|---|---|
| Motores | Backtest, WalkForward, MonteCarlo, Optimizer, Shadow, FeatureDiscovery, Promotion, KnowledgeBase | Backtest, WalkForward, MonteCarlo, Optimizer, AutoResearch, FeatureStore, KnowledgeGraph, ML pipeline |
| ML | Python puro (pesos logísticos) | scikit-learn, XGBoost, LightGBM, CatBoost |
| Datos | Lee `committee_outcomes` del Core en solo lectura | **Su propio Postgres y su propio colector DexScreener/Birdeye** |
| Features | `FeatureCatalog` (`basic.*`, `tech.*`, `holders.*`, `pool.*`, `regime.*`, `time.*`) | Su propio `feature_store` con nomenclatura propia |

Son dos plataformas de investigación con datos disjuntos, esquemas de features disjuntos y familias de
modelos incompatibles. La decisión sobre cuál es *la* plataforma de investigación es la decisión
arquitectónica más importante que este proyecto tiene pendiente, y se ha estado difiriendo.

### D8 — Hallazgos menores

- `QualitySignals` se construye desde defaults de configuración (`learning.default_dataset_quality`,
  `default_sample_support`, ambos 0.5) y **nunca se recalcula desde el dataset real**
  (`ops/committee_runtime.py:146-149`). Son constantes disfrazadas de señales.
- 2 de 6 fuentes del scanner (`jupiter`, `meteora`) responden 404 documentado. Reduce el caudal sin
  marcar nada como no-sano.
- El `frontend` no tiene un solo test.
- `docker-compose.override.yml` se auto-carga; ya se documentó, pero el pie de fallo sigue ahí para
  cualquiera que haga `docker compose up` sin `-f`.

---

## 4. Cuellos de botella

| # | Cuello | Naturaleza | Nota |
|---|---|---|---|
| B1 | Un solo proceso `worker` para todo el pipeline | CPU / event loop | El `engine` existe y está vacío: la salida ya está diseñada, falta ejecutarla |
| B2 | Research Lab interno co-residente con el hot path | CPU | Backtests CPU-bound contra descubrimiento en tiempo real |
| B3 | `contexts/research` y el Committee comparten la conexión a Postgres del worker | I/O | Un `PostgresOutcomeStore.load(limit=100_000)` en el training loop compite con el pipeline |
| B4 | Bus Redis Streams con **un consumer group por servicio y todos los servicios ven todos los eventos** | Red / deserialización | Cada proceso deserializa el 100% del tráfico aunque le interese el 2%. Escala mal por diseño |
| B5 | `docker` con `storage-driver: vfs` en el CT 203 | Disco | Ya causó una caída completa por disco lleno. **Sigue pendiente** |
| B6 | `DexScreenerPriceOracle` batchea 30 mints/request | Red externa | Aceptable hoy; es el límite duro del libro abierto |

**No verificado en vivo:** ninguno de estos cuellos se midió con perfilado en esta pasada. B1–B4 son
estructurales (deducibles del código); B5 está documentado por el incidente previo.

---

## 5. Problemas de cold start

Esta es la sección que más importa para el objetivo de "aprender continuamente".

### El deadlock, en su forma real

```
sin trades cerrados etiquetados
        │
        ▼
OutcomeStore = solo negativos (TokenRejected)   ◄── A1: record_outcome sin llamador
        │
        ▼
dataset de una sola clase  →  AUC indefinido
        │
        ▼
ValidationEngine.min_auc=0.55  →  ningún candidato pasa NUNCA
        │
        ▼
registry sin cards activas
        │
        ▼
_load_active_committee() no encuentra nada  →  default_committee() (priors)
        │
        ▼
P(ROI+) ≈ 0.4626 (meta bias −0.15 con opiniones neutras)  <  min 0.55
        │
        ▼
Risk rechaza  →  no se abre posición  →  no se cierra trade  ──┘
```

**Punto clave:** el ciclo tiene **dos** cierres necesarios, y hoy fallan ambos:

1. **El lazo de datos** (A1). Aunque se abrieran posiciones, sus resultados no llegarían al ledger.
2. **El lazo de despliegue** (A3). Aunque se entrenara un modelo bueno y se promocionara, el runtime no
   lo cargaría hasta el siguiente reinicio.

Cortar solo uno no arranca el sistema. Y ninguno de los dos se arregla bajando umbrales.

### Los tres arranques en frío distintos

| Arranque | Qué falta al principio | Cómo debería resolverse |
|---|---|---|
| **Modelos** | Sin muestras positivas, ningún modelo puede entrenarse | Fase de bootstrap deliberada: paper trading con una política de entrada explícitamente *no basada en el comité* (p.ej. reglas del Security Engine + liquidez), cuyo único propósito es generar etiquetas. Requiere que A1 esté cerrado primero |
| **Wallet Intelligence** | La reputación arranca vacía; el "suelo de scammer" y el trinquete de experiencia necesitan historia | Backfill desde el Research Lab o aceptar un periodo de warm-up declarado |
| **Régimen de mercado** | El clasificador de 7 regímenes necesita serie histórica | Alimentable desde datos históricos sin esperar |

### El coste de silencio

Con `confidence` penalizada multiplicativamente por volatilidad (hasta −40%) y `min_confidence = 0.40`,
un especialista que **se abstiene devuelve exactamente 50.0** (`committee/base.py:135`). En cobertura
baja de features —el caso normal en un token recién lanzado, que es *el caso de uso entero de la
plataforma*— el comité se apelmaza en el centro. El diseño trata la abstención como un valor neutro,
pero la política aguas abajo lo lee como una medición. Eso merece una decisión explícita: una abstención
no es un 50, es *ausencia de dato*, y las políticas deberían poder distinguirlas.

---

## 6. Componentes que nunca terminan conectándose

Inventario cerrado, con evidencia:

| Componente | Estado | Evidencia |
|---|---|---|
| `KnowledgeFeedback.record_outcome` | Definido, **cero llamadores** | `knowledge_feedback.py:54` |
| `EnsembleSignalGenerated` | Publicado, **cero suscriptores** | `strategy/application/engine.py:159` |
| `contexts/scoring` completo | **Cero referencias externas** | `grep contexts.scoring` → solo un comentario |
| `contexts/wallet` completo | **Cero referencias externas** | `grep contexts.wallet` → vacío |
| `strategy.gate_risk` | Flag leído solo para logs/snapshots | `settings.py:676`, `strategy_runtime.py:83,104` |
| Proceso `engine` | Supervisado, sin lógica | `ops/engine.py:26` |
| `ModelPromoted` → runtime | Evento publicado y auditado, **nunca aplicado** | `ops/committee_runtime.py:177` |
| Store Postgres de eventos | Rama inexistente | `bootstrap.py:314` |
| Exportador de candidatos del Lab | **No existe en ninguno de los dos lados** | §7 |
| Adaptadores live (signer / quote / RPC) | No existen | intencional |

---

## 7. El puente con el Research Lab — es peor que "falta un exportador"

El diagnóstico anterior registrado en Notion decía que falta el exportador en `HadesResearchLab`. Es
cierto, pero incompleto. El puente es **estructuralmente incompatible en tres dimensiones a la vez**:

### 7.1 Formato

- **El Core acepta**: JSON `hades.candidate/v1`, `model_kind` obligatoriamente `"logistic"`, un conjunto
  de pesos `{bias, coefficients{feature: peso}}`, checksum canónico, nombres restringidos a los 12
  especialistas + meta-model (`contexts/learning/domain/candidates.py:45-56, 239-241`).
- **El Lab produce**: un directorio con `model.pkl` + `preprocessor.pkl` + `manifest.json`
  (`hades_research/ml/packaging.py:10-18`).

No es que falte un traductor: **el Core rechaza pickles por diseño explícito y correcto**
(*"Unpickling is code execution"*, `candidates.py:14`).

### 7.2 Familia de modelos

El Lab entrena XGBoost, LightGBM, CatBoost, Random Forest, GradientBoosting y LogisticRegression
(`hades_research/ml/models.py`). **Solo la última es expresable** como `sigmoid(bias + Σwᵢxᵢ)`. Los
modelos de árboles del Lab **no pueden cruzar el puente en ninguna forma**, hoy ni nunca, sin romper la
regla de oro de explicabilidad del Core.

Es decir: el Lab está construyendo, con su stack más potente, artefactos que el Core está diseñado para
rechazar.

### 7.3 Espacio de features

El Core normaliza contra un `FeatureCatalog` con namespaces `basic.*`, `tech.*`, `holders.*`, `pool.*`,
`regime.*`, `time.*` (`feature_catalog.py`). El Lab tiene su propio `feature_store` con su propia
nomenclatura, alimentado por **su propio colector** contra DexScreener/Birdeye, en **su propio Postgres**
(`hades_research/core/config.py`, `data_collection/sources/live.py`).

Los dos sistemas observan el mismo mercado y construyen dos representaciones incompatibles de él. Aun
resolviendo 7.1 y 7.2, un vector de pesos del Lab indexaría features que el Core no sabe producir.

### 7.4 La decisión que hay que tomar

Hay tres salidas, y son mutuamente excluyentes. Esta es la pregunta principal que devuelvo antes de
escribir código:

| Opción | Qué implica | Coste | Preserva explicabilidad |
|---|---|---|---|
| **O1 — El Lab se somete al contrato del Core** | El Lab restringe su *salida de producción* a modelos logísticos sobre el `FeatureCatalog` del Core. Sus árboles quedan como herramienta de descubrimiento (qué features importan), no de despliegue | Bajo. Un exportador + adoptar el catálogo del Core | **Sí, intacta** |
| **O2 — El Core acepta modelos opacos** | Añadir un runtime de inferencia (ONNX o similar) y una capa de explicabilidad post-hoc (SHAP) | Alto. Rompe "sin ML pesado en runtime", añade superficie de ejecución de artefactos externos | **No.** Explicabilidad pasa a ser aproximada |
| **O3 — Absorber el Lab en el Core** | `contexts/research` es ya el 80% del Lab. Retirar `HadesResearchLab` y quedarse con un solo stack | Medio-alto. Se pierde el ML pesado y el AutoResearch | Sí |

**Recomendación:** **O1**. Es la única que preserva la propiedad más valiosa del proyecto y la única
que resuelve las tres incompatibilidades con un solo cambio de política, no de arquitectura. Los árboles
del Lab siguen siendo enormemente útiles como *generadores de hipótesis* — descubren interacciones y
rankean features — y esa salida sí cruza el puente sin comprometer nada.

Pero es una decisión de producto, no mía. **Requiere tu confirmación antes de la Fase 3 del plan (§11).**

---

## 8. Flujo de eventos — documentado vs. real

### 8.1 El flujo que dice `docs/architecture.md`

```
Scanner → Features → Security → Intelligence → Committee → Scoring → Strategy → Risk → Execution → Portfolio
```

### 8.2 El flujo que ejecuta el código

```
Scanner ──TokenDiscovered/MetadataCollected──► Features
Features ──FeaturesComputed──► Security ──► Intelligence ──WalletIntelligenceComputed──► Committee
Committee ──CommitteePredictionGenerated──┬──► Risk ──TradeApproved──► Execution ──► Portfolio
                                          └──► Strategy ──EnsembleSignalGenerated──► ✗ NADIE

Security ──SecurityScoreComputed──────┐
Intelligence ──WalletIntelligence─────┴──► EventDrivenRiskFacts (caché lateral) ──► Risk

Portfolio ──PositionOpened/Updated/Closed──► PositionMonitor · PortfolioManager · Strategy · Risk
                                          └──► ✗ Learning  (A1: el lazo abierto)

Security ──TokenRejected──► KnowledgeFeedback ──► OutcomeStore  (solo negativos)

Features ──FeaturesComputed──► Research (shadow virtual)
```

**Tres discrepancias con la documentación:**

1. **`Scoring` no existe en el flujo.** No hay etapa entre Committee y Strategy.
2. **`Strategy` no está entre Committee y Risk.** Está *en paralelo*, y su salida se descarta.
3. **El retorno de Portfolio a Learning no existe.** El diagrama no lo muestra, pero la narrativa de
   `hades.md` §6e ("knowledge feedback — aprende de ejecutados y rechazados") afirma que sí.

`docs/architecture.md` se actualiza en este mismo commit para reflejar el flujo real.

---

## 9. Dónde se pierde información

Ordenado por gravedad. "Se pierde" = un dato se produce, es correcto, y ningún consumidor lo persiste
ni lo usa.

| # | Qué se pierde | Dónde se produce | Dónde muere | Impacto |
|---|---|---|---|---|
| **P1** | **El resultado realizado de cada trade** (ROI, TP/SL alcanzado) | `PositionClosed` | Nadie lo convierte en `TrainingSample` | **Impide todo aprendizaje.** El activo más caro de generar del sistema se descarta |
| **P2** | La opinión completa de las 15 estrategias y su ensemble ponderado | `EnsembleSignalGenerated` | Sin suscriptor | La Fase 10 entera no influye en nada |
| **P3** | El historial completo de eventos de dominio | Todo el bus | `InMemoryEventStore` | Sin post-mortem posible. Ya costó un incidente indeterminable |
| **P4** | La calidad real del dataset y el soporte muestral | `Dataset.positive_rate`, `.size` (se loguean en `dataset_builder.py:47-54`) | Nunca alimentan `QualitySignals` | La confianza del comité se calcula sobre constantes |
| **P5** | Los outcomes de estrategia (`StrategyOutcome`) | `strategy/subscriber.py:73` | Van al `PerformanceStore` de strategy, **no al `OutcomeStore` de learning** | Dos ledgers de resultados que no se hablan |
| **P6** | Las features del momento exacto de la decisión | Feature store | Al cerrar la posición se re-consultarían las features *actuales*, no las de entrada | Riesgo de **label leakage** cuando se cierre P1. Debe resolverse con snapshot en la entrada |
| **P7** | Todo lo que el `HadesResearchLab` aprende | Su propio Postgres | No cruza | §7 |

**P6 merece atención especial en el diseño de la solución a P1.** La forma ingenua de cerrar el lazo
—suscribir `PositionClosed` y pedir `feature_store.latest(token)`— introduciría fuga temporal: se
entrenaría con las features del momento de *salida* etiquetadas con el resultado del trade. El snapshot
de features debe capturarse en `TradeApproved` y viajar con la posición.

---

## 10. Riesgos arquitectónicos

| # | Riesgo | Prob. | Impacto | Mitigación propuesta |
|---|---|---|---|---|
| **R1** | Se "arregla" el cold start bajando umbrales en vez de cerrando el lazo | **Alta** | **Crítico** — la plataforma operaría a ciegas con la apariencia de funcionar | Cerrar A1 y A3 *antes* de tocar un solo umbral. Registrarlo como regla del proyecto |
| **R2** | Se cierra P1 sin resolver P6 y se entrena con fuga temporal | Alta | Crítico — modelos con métricas excelentes y comportamiento pésimo en vivo | Snapshot de features en la entrada, no en la salida. Test que lo fije |
| **R3** | Añadir ML pesado al Core para cerrar el puente | Media | Alto — destruye la explicabilidad y la pureza del runtime | Decidir §7.4 explícitamente antes de escribir código |
| **R4** | El worker monolítico se satura y se degrada de forma no observable | Media | Alto | Sacar Research y luego el pipeline al proceso `engine`, que ya existe para eso |
| **R5** | Sin event store durable, el próximo incidente vuelve a ser indeterminable | **Alta** | Alto | H1: event store en Postgres |
| **R6** | Deriva documentación↔código (ya materializada: §8) | **Ya ocurrió** | Medio | Test que verifique que todo evento del diagrama tiene productor *y* consumidor |
| **R7** | `vfs` en el CT 203 vuelve a llenar el disco | **Alta** | Alto | `overlay2`. Pendiente desde el incidente |
| **R8** | API key de Helius comprometida sin rotar | **Confirmada** | Alto | **Rotar. Pendiente desde 2026-07-27** |
| **R9** | Dos plataformas de investigación divergen más con cada sesión | Alta | Medio-alto | Decidir §7.4 |
| **R10** | Contextos muertos (`scoring`, `wallet`) inducen a error a futuros lectores | Alta | Bajo-medio | Retirar o documentar explícitamente como reservados |

---

## 11. Plan de evolución por fases

Principio rector: **ninguna fase introduce capacidad nueva antes de que la capacidad existente esté
conectada.** El orden no es negociable — cada fase depende de la anterior para poder validarse.

### Fase 0 — Higiene operativa (fuera del código, bloquea todo lo demás)

- Rotar la API key de Helius (**R8**, pendiente desde 2026-07-27).
- `vfs` → `overlay2` en el CT 203 (**R7**).
- Desplegar `6e50cb8` (el CT está en `10d9186`).

Sin esto no hay entorno donde validar nada de lo que sigue.

### Fase 1 — Cerrar el lazo de aprendizaje *(el desbloqueo)*

**Objetivo:** que un trade cerrado produzca una muestra de entrenamiento correctamente etiquetada y sin
fuga temporal.

1. Capturar el snapshot de features **en la entrada** (`TradeApproved` → tags de la posición), resolviendo
   **P6/R2** antes de que exista la ruta de escritura.
2. Suscribir el contexto `learning` a `PositionClosed` y llamar a `record_outcome` con ese snapshot y las
   etiquetas realizadas (**P1/A1**).
3. Alimentar `QualitySignals` desde el `Dataset` real en vez de constantes (**P4**).
4. Suscribir `ModelPromoted` → `set_active()` para que una promoción surta efecto en caliente (**A3/D3**).

**Criterio de salida:** un trade cerrado en paper aparece como `TrainingSample` con
`was_executed=True` y las features del instante de la decisión. Verificable con un test de integración
sin stack.

### Fase 2 — Arranque en frío deliberado

**Objetivo:** generar las primeras etiquetas positivas sin pedirle al comité que decida antes de saber.

1. Definir una **política de bootstrap** explícita y temporal, no basada en el comité (Security Engine +
   liquidez + reglas duras), cuyo propósito declarado es generar dataset. Con presupuesto de capital
   propio y apagado automático al alcanzar N outcomes.
2. Distinguir **abstención de medición** en el comité y en las políticas de Risk (§5, el "coste de
   silencio"). Una abstención no es un 50.
3. Solo entonces recalibrar umbrales, con datos.

**Criterio de salida:** `ValidationEngine` recibe un dataset de dos clases y un candidato pasa el
gauntlet por sus propios méritos.

### Fase 3 — Decidir y cerrar el puente con el Research Lab

**Bloqueada por tu decisión sobre §7.4.** Bajo la recomendación **O1**:

1. El Lab adopta el `FeatureCatalog` del Core como espacio de features de producción.
2. Exportador `hades.candidate/v1` en `HadesResearchLab` (pesos logísticos + checksum canónico).
3. Los modelos de árbol del Lab quedan como generadores de hipótesis (ranking de features → propuestas),
   no como artefactos desplegables.
4. Test de contrato **en ambos repos** contra el mismo fixture, para que el formato no pueda derivar.

### Fase 4 — Conectar el Strategy Engine o retirarlo

**Requiere decisión.** El Strategy Engine es demasiado grande para dejarlo desconectado indefinidamente.
Dos salidas honestas:

- **Conectarlo**: implementar `gate_risk` de verdad — Risk pasa a consumir `EnsembleSignalGenerated` en
  vez de (o además de) `CommitteePredictionGenerated`, y las señales `SELL`/`EXIT` obtienen un consumidor.
- **Congelarlo**: marcarlo explícitamente como advisory/experimental en la documentación y sacarlo del
  diagrama del pipeline.

Lo que no es aceptable es el estado actual: presentado como una etapa del pipeline sin serlo.

### Fase 5 — Durabilidad y trazabilidad

1. Event store en Postgres (**H1/D5/P3**) — sin esto, ningún incidente futuro será diagnosticable.
2. Ledger durable de órdenes/transacciones (**H2**).
3. Retirar o documentar `contexts/scoring` y `contexts/wallet` (**D4/R10**).
4. Test de coherencia diagrama↔código: todo evento del mapa debe tener productor y consumidor (**R6**).

### Fase 6 — Escalabilidad

1. Sacar `contexts/research` del worker a su propio proceso (**B2**).
2. Mover el pipeline de decisión al proceso `engine`, que existe exactamente para esto (**B1/R4**).
3. Suscripción selectiva por servicio en el bus Redis en vez de "todos ven todo" (**B4**).

### Fase 7 — Pre-LIVE

Fuera del alcance de esta evolución. Requiere su propia sesión: `TransactionSigner`, quote provider,
cableado, y auditoría independiente. **No debe iniciarse hasta que las Fases 1–5 estén cerradas y
validadas en paper con datos reales.**

---

## 12. Qué necesito de ti antes de escribir código

1. **§7.4 — la decisión del puente** (O1 / O2 / O3). Bloquea la Fase 3 y condiciona toda la estrategia de
   ML del proyecto. Mi recomendación es **O1**.
2. **§Fase 4 — Strategy Engine**: ¿conectar o congelar?
3. **Confirmación del orden de fases.** En particular, que la Fase 1 va antes que cualquier recalibración
   de umbrales (**R1**).

---

## 13. Trazabilidad de este documento

Todo hallazgo de §3, §6 y §9 es verificable en frío sobre `6e50cb8` con `grep`/lectura, sin stack.
Lo **no verificado en vivo** y marcado como tal: los cuellos de botella de §4 (estructurales, no
perfilados) y el comportamiento en producción posterior al despliegue de `6e50cb8`, que aún no está en
el CT 203.
