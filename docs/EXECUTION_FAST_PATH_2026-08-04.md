# Ejecución rápida y anti-MEV — opciones concretas

**Fecha:** 2026-08-04 · **Rama:** `claude/hades-bot-wiring-issues-8bcbnj` · **HEAD:** `31c1ced`
**Alcance:** checklist §1, primer punto — *investigar y documentar* opciones de ejecución de baja
latencia y protección anti-MEV para el camino live de Hades.

**Este documento no implementa ni activa nada.** Es el insumo para decidir qué `ExecutionAdapter`
construir (§1 punto 2 del checklist) y, más adelante, si algo de esto se enciende con plata real.
Cada opción que implique gasto está marcada como **decisión tuya** según las reglas de la tarea.

---

## 0. Punto de partida — qué existe hoy en el repo

Antes de comparar proveedores, el estado real del camino de ejecución, verificable en frío:

| Pieza | Estado | Evidencia |
|---|---|---|
| `Executor` (puerto, la costura paper↔live) | Existe y es el único seam de modo | `contexts/execution/domain/ports.py:24-37` |
| `PaperExecutor` | Construido siempre, es el default | `application/factory.py:99-106` |
| `LiveExecutor` | **Clase implementada**, secuencia `quote → slippage → sign → send → confirm` | `application/live_executor.py:75-120` |
| `QuoteProvider` (puerto) | Definido, **cero implementaciones** | `ports.py:68-80`, sin adaptador en `infrastructure/` |
| `TransactionSigner` (puerto) | Definido, **cero implementaciones** | `ports.py:41-51` |
| Resultado neto | `_maybe_build_live()` devuelve `None` por colaboradores ausentes | `factory.py:144-151` |
| Jito | **Solo como línea de coste estimado**, no hay envío por bundles | `ExecutionSettings.jito_enabled=False`, `jito_tip_microlamports=0` (`settings.py:470-471`); se usa únicamente en `FeeEngine` |
| Jupiter | Se usa hoy para *simular honeypots* y para la lista de tokens nuevos, **no para ejecutar** | `security/infrastructure/swap_simulator.py:39`, `scanner/.../jupiter.py:30` |

**Consecuencia importante para el checklist:** la frase "no reemplaces el adapter actual" tiene una
lectura precisa acá — el adapter *live* actual no existe todavía. Lo que sí existe y no se toca es
`PaperExecutor` y el contrato `Executor`. Todo lo que sigue entra **detrás** de ese puerto, nunca
esquivándolo, y por tanto sigue pasando por el Risk Manager como único constructor de `TradeApproved`
(`contexts/risk/application/manager.py:305`).

Hay además una medición que **hoy no se toma**: `FillReport.latency_ms` mide el tiempo de pared del
método `execute()` (`live_executor.py:97`), que en paper es una latencia simulada por configuración
(`paper.simulated_latency_ms`). No hay ninguna métrica de *landing* de transacción — el tiempo desde
que se firma hasta que la tx aparece confirmada en un slot. Esa es la métrica que el checklist pide
loguear y la que ninguna de las opciones de abajo se puede comparar sin ella.

---

## 1. Cómo se aterriza una transacción en Solana hoy (2026)

El mercado cambió lo suficiente desde que se escribió el diseño original como para que valga
recapitularlo, porque condiciona las opciones.

- El cliente **Jito-Solana corre en más del 95% del stake activo**, y los tips de Jito representan
  más del 60% del volumen de priority fees de la red. Es decir: la "subasta fuera de cadena" de Jito
  ya no es una vía alternativa, es la vía principal.
- Un **bundle** de Jito es un paquete atómico de hasta 5 transacciones firmadas, con el tip como
  última instrucción (transferencia de SOL a una de las cuentas de tip). El tip va directo al líder
  del bloque, lo que le da a *ese* validador el incentivo de priorizar el bundle.
- La API JSON-RPC del Block Engine expone `sendBundle`, `getBundleStatuses`,
  `getInflightBundleStatuses`, `getTipAccounts`, `getRandomTipAccount`, y un proxy `sendTransaction`.
  Hay clusters regionales (Ámsterdam, Dublín, Fráncfort, Londres, Nueva York, Salt Lake City,
  Singapur, Tokio) además de los front-doors globales.
- **La subasta es continua y adaptativa.** Los tips que funcionaban la semana pasada no funcionan
  esta. Los equipos que lo hacen en serio calibran contra telemetría de tips de bloques recientes en
  vez de hardcodear un valor. Esto es directamente relevante: el `jito_tip_microlamports` de Hades es
  hoy una constante de configuración, y una constante es exactamente lo que no funciona acá.

**Lectura para Hades:** cualquier "priority fee dinámico" que implementemos tiene que leer telemetría
de tips reciente, no interpolar sobre un valor fijo. Eso es una fuente de datos nueva y un bucle de
muestreo, no un parámetro.

---

## 2. Las cuatro opciones de envío, comparadas

Ordenadas por coste incremental y por cuánta superficie de riesgo nueva introducen.

### O1 — Jupiter (agregador) por el camino estándar

Lo que el diseño ya presupone. `QuoteProvider` habla con Jupiter, `TransactionSigner` firma, se manda
por RPC normal.

| | |
|---|---|
| **Coste** | Cero incremental. Ya se usa Jupiter para el simulador de honeypots |
| **Latencia** | La peor de las cuatro: sin SWQoS ni subasta, la tx compite en el mempool público |
| **Anti-MEV** | Jupiter cancela la operación si el precio ejecutado se sale de la tolerancia — protege contra el *daño* del sandwich en pools finos, no impide el sandwich |
| **Riesgo nuevo** | Ninguno. Es el camino que el código ya asume |
| **Decisión tuya** | No. Es el default |

Jupiter tiene además **slippage dinámico** (heurísticas que calculan el umbral por operación en vez
de un valor fijo) y, en **Ultra v3**, afirma 34x mejor protección anti-sandwich, mejor slippage y
hasta 10x menos fees de ejecución, con soporte para pares memecoin-memecoin y mínimo de trade de $10.
Esas cifras son de Jupiter, no medidas por nosotros — es exactamente el tipo de afirmación que el
modo shadow del punto 3 del checklist existe para verificar.

### O2 — Helius Sender (routing dual SWQoS + Jito)

Un endpoint de envío que despacha **en paralelo** por conexiones staked (SWQoS) y por la subasta de
Jito, con routing geográfico en siete regiones. Es la opción con mejor relación coste/latencia hoy.

| | |
|---|---|
| **Coste** | **No consume créditos de API**; se paga por transacción con un tip en SOL. Disponible incluso en el plan gratuito |
| **Tip** | Sender Max: mínimo **0.001 SOL** (entra al buffer de tip prioritario, top-of-block; más tip = antes). Tier económico solo-SWQoS: **0.000005 SOL** |
| **Latencia** | La mejor sin colocation: dos caminos de aterrizaje en paralelo |
| **Anti-MEV** | Indirecto: por Jito, inclusión optimizada; no es un escudo anti-sandwich por sí mismo |
| **Riesgo nuevo** | Dependencia de un proveedor para el envío. Mitigable: el envío por RPC normal queda como fallback |
| **Decisión tuya** | **Sí, parcialmente.** El tier de 0.000005 SOL está dentro de los defaults actuales; el tier Max de 0.001 SOL/tx es gasto por encima de los defaults y entra en la lista de "no decido yo" |

A 0.001 SOL por transacción y con SOL a ~$150 (el valor que usa `ExecutionSettings.sol_price_usd`),
son **~$0.15 por operación**. En una operativa de scalping con, digamos, 200 operaciones diarias
(entrada + salida = 400 txs), eso es **~$60/día, ~$1.800/mes** solo en tips. Ese número es el que
manda en la decisión, no la latencia.

### O3 — Jito directo, con bundle y tip dinámico

Hablar con el Block Engine nosotros mismos: `getTipAccounts` para las cuentas de tip, telemetría de
tips recientes para calibrar, `sendBundle` para el envío atómico, `getBundleStatuses` para confirmar.

| | |
|---|---|
| **Coste** | El tip, calibrado dinámicamente. Sin cuota fija |
| **Latencia** | Comparable a O2 por el lado Jito, sin el camino SWQoS en paralelo |
| **Anti-MEV** | **La ventaja real: atomicidad.** Un bundle de hasta 5 txs se incluye entero o no se incluye. Eso permite construcciones que un envío suelto no permite (p.ej. swap + verificación de saldo en el mismo paquete), y quita la ventana entre txs que un sandwich necesita |
| **Riesgo nuevo** | Más código propio: calibración de tips, manejo de bundles no incluidos (un bundle que no entra no falla — simplemente no pasa nada, y hay que detectarlo), y el bucle de telemetría |
| **Decisión tuya** | **Sí.** Cualquier tip por encima de los defaults actuales (`jito_tip_microlamports=0`) es gasto que no decido yo |

La atomicidad es lo que hace que esta opción no sea redundante con O2. Es también la más cara en
esfuerzo de implementación y la que más superficie de fallo silencioso añade — precisamente la clase
de defecto que este repo lleva meses cazando.

### O4 — Interacción directa con pools de AMM (Raydium / Orca)

Saltarse el agregador y construir la instrucción de swap contra el pool directamente.

| | |
|---|---|
| **Coste** | Cero en fees de terceros; se ahorra el overhead del agregador |
| **Latencia** | Menor por un tramo: se elimina el round-trip de cotización al agregador (hoy, contra `lite-api.jup.ag`) |
| **Anti-MEV** | **Ninguna.** Al contrario: se pierde el ruteo y el slippage dinámico de Jupiter |
| **Riesgo nuevo** | **Alto, y cualitativamente distinto.** Se asume el ruteo, el descubrimiento de pools, la matemática de la curva y la validación de que el pool es el correcto. Un pool equivocado o un cálculo de mínimo-recibido mal hecho es pérdida directa de capital, sin la red de contención del agregador |
| **Decisión tuya** | **Sí, explícitamente** — el checklist lo lista: "si eso cambia la superficie de riesgo de ejecución". La cambia, y bastante |

**Mi recomendación sobre O4: no, todavía.** El argumento a favor es un ahorro de latencia de un solo
round-trip HTTP; el argumento en contra es asumir el ruteo y la validación de pools en un contexto
—memecoins recién lanzadas— donde el pool malicioso *es el escenario esperado*, y donde el Security
Engine de Hades existe justamente porque asumimos que la contraparte es hostil. Vale la pena medir
primero cuánto de la latencia total es realmente ese round-trip. Si resulta ser el 5%, la pregunta se
cierra sola.

---

## 3. Protección anti-sandwich — qué se puede hacer de verdad

Conviene separar tres cosas que se mezclan bajo "anti-MEV":

1. **Evitar el sandwich.** Solo lo logra la inclusión privada/atómica (bundles de Jito, O3). Si tu tx
   nunca pasa por un mempool público observable, no hay dónde ponerle pan.
2. **Acotar el daño del sandwich.** Es lo que hace el slippage: si el precio ejecutado se sale de la
   tolerancia, la operación se cancela. Hades ya tiene esto y con tres niveles
   (`slippage_bps=100` base, `max_slippage_bps=300` por orden, `hard_max_slippage_bps=500` techo
   absoluto, `settings.py:460-462`) más un `SlippageEngine` dedicado. **Esta capa ya está construida
   y es la más importante.**
3. **Detectar que te pasó.** Nadie lo hace hoy en Hades. Comparar el precio cotizado con el precio
   ejecutado y registrar la diferencia es barato y es la única forma de saber si el problema existe.

Un dato que ordena las prioridades: un análisis de Helius de enero de 2025 contó ataques de sandwich
en **112 de 150 tokens DeFi** operados en una semana, y aproximadamente la mitad de esos ataques
**quedaron en break-even o en pérdida para el atacante** pese a que las víctimas tenían slippage
"aceptable" del 2%. Es decir: el sandwich es omnipresente pero frecuentemente poco rentable en pools
finos. La cifra que suele citarse de coste con y sin protección (≈3% vs ≈0.7% en un swap de $200) da
el orden de magnitud del ahorro potencial: **~2 puntos porcentuales por operación**.

**Contra esos 2 puntos hay que poner el coste del tip.** En una operación de $50 (el rango en que
opera hoy el Exploration Mode), 2 puntos son $1.00 y el tip Max de Sender son $0.15 — favorable. En
una operación de $5, 2 puntos son $0.10 contra $0.15 de tip — **el remedio cuesta más que la
enfermedad**. La conclusión no es "activar anti-MEV" sino que **la protección tiene que ser función
del tamaño de la orden**, y ese es un parámetro que hoy no existe.

---

## 4. Lo que falta medir antes de elegir

Ninguna de las comparaciones de arriba se puede resolver con literatura. Lo que decide es:

| Métrica | Hoy | Cómo obtenerla |
|---|---|---|
| Latencia de landing punta a punta (firma → confirmación en slot) | **No se mide** | Instrumentar el nuevo adapter; es el punto 2 del checklist |
| Desglose de esa latencia (cotización / firma / envío / confirmación) | No se mide | Lo mismo, por tramos |
| Tasa de fill y precio cotizado vs. ejecutado | No se mide | Registrar ambos en `FillReport` |
| Cuánto del total es el round-trip de cotización | No se mide | Decide sola la pregunta de O4 |
| Distribución de tips reciente | No se consulta | `getTipAccounts` + telemetría de bloques |

Por eso el orden del checklist es el correcto: el adapter instrumentado (punto 2) y el modo shadow
(punto 3) vienen **antes** que cualquier decisión de gasto. El shadow permite medir O1 contra O2/O3
en paper, con órdenes reales del pipeline, sin arriesgar un centavo.

---

## 5. Recomendación

1. **Construir el adapter nuevo contra el puerto `Executor` existente**, con instrumentación de
   latencia por tramos como su razón de ser principal. Detrás de un flag apagado por default.
2. **Empezar por O1 + O2 (Jupiter cotizando, Helius Sender enviando)**, que es la combinación que da
   la mayor mejora de latencia por el menor coste y la menor superficie de riesgo nueva. El tier
   solo-SWQoS (0.000005 SOL) cabe dentro de los defaults actuales.
3. **Correrlo en shadow contra el camino actual** y traer números reales.
4. **Diferir O3 (Jito directo con bundles)** hasta tener el número de cuánto sandwich estamos comiendo
   realmente. Es la opción técnicamente superior y la única que *evita* en vez de *acotar*, pero su
   valor depende de un dato que no tenemos.
5. **No hacer O4 (AMM directo)** por ahora, por el argumento de §2.
6. **Hacer del tamaño de orden un input de la política de protección**, por la aritmética de §3.

### Decisiones que quedan para vos

- Tier de Helius Sender: **solo-SWQoS (0.000005 SOL/tx, dentro de defaults)** vs. **Max (0.001 SOL/tx
  ≈ $0.15, ~$1.800/mes a 400 tx/día)**.
- Si se implementa O3, el techo del tip dinámico — hoy el default es 0.
- Si en algún momento se abre la puerta a O4 (AMM directo), que cambia la superficie de riesgo.
- Contratar RPC dedicado o colocation → es el punto §6 del checklist, se documenta aparte.

Nada de esto se enciende sin tu respuesta. Mientras tanto, lo que sigue es el punto 2: el adapter
instrumentado, apagado.

---

## Fuentes

- [Jito Explained: Bundles, Tips, and How MEV Works on Solana in 2026 — Chainstack](https://chainstack.com/jito-explained-bundles-tips-mev-solana/)
- [Jito Explained: Bundles, Tips & Solana MEV in 2026 — RPC Fast](https://rpcfast.com/blog/jito-explained-bundles-tips-mev-solana)
- [Helius Sender: Ultra-Low Latency Solana Transaction Submission — Helius Docs](https://www.helius.dev/docs/sending-transactions/sender)
- [Achieving Zero-Slot Execution with Sender and LaserStream — Helius](https://www.helius.dev/blog/zero-slot)
- [Staked Connections — Helius](https://www.helius.dev/staked-connections)
- [Solana MEV Report: Trends, Insights, and Challenges — Helius](https://www.helius.dev/blog/solana-mev-report)
- [Jupiter unveils Ultra v3 — The Block / TradingView](https://www.tradingview.com/news/the_block:724a27a93094b:0-solana-decentralized-exchange-aggregator-jupiter-unveils-ultra-v3-offering-improved-trade-execution-mev-protections-and-gasless-support/)
- [Jupiter Swap | Fees, Slippage & Tips (2026)](https://uwuu.ai/blog/jupiter-swap)
- [Solana Trading Fees 2026: Slippage, Priority Fees, MEV — ManagerNest](https://managernest.com/blog/solana-trading-fees-explained-2026)
