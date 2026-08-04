# Auditoría de ingesta — ¿polling o push?

**Fecha:** 2026-08-04 · **Rama:** `claude/hades-bot-wiring-issues-8bcbnj`
**Alcance:** checklist §2, primer punto — *auditar* cómo descubre tokens y cómo actualiza precios
el Scanner hoy. Verificable en frío con `grep`/lectura, sin stack.

**Respuesta corta: todo es polling por intervalo. No hay una sola conexión push en el sistema.**

---

## 1. Descubrimiento de tokens

### El camino real

```
HttpPollingSource.stream()  ──(cada 5 s, HTTP GET)──►  DiscoveryEngine._handle()
    │                                                         │
    │  dedup + gate coarse (liquidez mínima, listas)           │
    ▼                                                         ▼
AcquisitionPipeline (cola con backpressure, worker pool)  ──► TokenDiscovered
```

| Aspecto | Estado | Evidencia |
|---|---|---|
| Transporte | **HTTP GET en bucle**, no websocket | `sources/base.py:117-123` — `stream()` es `while True: fetch → yield → asyncio.sleep(interval)` |
| Intervalo | **5 segundos** (`SCANNER_POLL_INTERVAL_SECONDS`) | `settings.py:228` |
| Fuentes activas | **3**: `pumpfun`, `raydium`, `dexscreener` | `settings.py:229` |
| Fuentes implementadas pero no activas | `jupiter`, `orca`, `meteora` | `sources/` — dos de ellas con 404 documentado en la auditoría de julio |
| Timeout por request | 12 s | `settings.py:239` |
| Resiliencia | Buena: tarea supervisada por fuente, backoff exponencial 1→60 s, callback de finalización | `discovery_engine.py:88-127` |

**El seam para streaming ya existe y está documentado.** `HttpPollingSource` dice explícitamente
que *"a subclass that instead subscribes to a WebSocket can implement `stream()` directly — the port
only requires `name` + `stream`"* (`sources/base.py:9-11`). El puerto `TokenSource` pide únicamente
`name` y `stream()`, un `AsyncIterator`. **Una fuente por websocket no requiere ningún cambio de
arquitectura**: entra como una implementación más al lado de las seis existentes, y el
`DiscoveryEngine` la supervisa igual que a las demás sin enterarse de la diferencia.

Eso es una buena noticia para el punto 2 del checklist: la instrucción de "no reemplaces el Scanner,
agregá una fuente adicional" es exactamente lo que la arquitectura ya prevé.

### La latencia estructural que esto impone

Un token nuevo no se puede descubrir antes de que:

1. la fuente upstream lo exponga en su endpoint (latencia del proveedor, desconocida),
2. **más** el tiempo hasta el siguiente tick de nuestro poll (**0 a 5 s, media 2.5 s**),
3. más el request en sí.

El punto (2) es un piso de ~2.5 s de media que ninguna optimización aguas abajo puede recuperar.
Para una operativa de scalping sobre memecoins recién lanzadas, donde la ventana entera puede ser de
segundos, ese piso es el número que importa — y es el único de los tres que está enteramente en
nuestras manos.

---

## 2. Actualización de precios

También polling, por un camino distinto y con su propia caché.

| Aspecto | Estado | Evidencia |
|---|---|---|
| Proveedor | DexScreener, endpoint batch de tokens | `market/infrastructure/price_oracle.py:34` |
| Transporte | HTTP GET bajo demanda | `DexScreenerPriceOracle` |
| Caché | **TTL 15 s** | `price_oracle.py:43` |
| Lote | **30 mints por request** | `price_oracle.py:45` |
| Quién lo consume | `PaperExecutor` (precio de referencia del fill) y `PositionMonitor` (marcar a mercado) | docstring `price_oracle.py:3-8` |
| Cadencia del Position Monitor | **5 s** (`EXECUTION_POSITION_MONITOR_INTERVAL_SECONDS`) | `position_monitor.py:111` |

**El precio que decide un take-profit puede tener hasta 15 segundos de antigüedad**, por la caché.
El diseño es correcto para lo que hace hoy (el endpoint es no autenticado y marcar un libro entero
request-por-request sería un problema de rate limit — el docstring lo razona bien), pero es
incompatible con scalping: en una memecoin, 15 segundos es un movimiento completo.

Nada de esto es profundidad de libro ni flujo de órdenes: es **un precio agregado, ya digerido por
DexScreener**. No hay bid/ask, no hay tamaño, no hay trades individuales. El checklist pide
justamente eso — profundidad de liquidez y flujo de órdenes — y hoy **no existe ninguna fuente en el
sistema que lo provea**.

---

## 3. Qué se mide hoy del tiempo discovered → features-ready

El punto 2.3 del checklist pide medir esto antes y después. El estado de partida:

| Métrica | Existe | Qué mide realmente |
|---|---|---|
| `hades_scanner_analysis_seconds` | **Sí**, observada | Tiempo de pared para procesar **un token dentro del pipeline** (`pipeline.py:157`) |
| `hades_scanner_stage_seconds{stage}` | **Sí**, observada | Por etapa: `storage_token`, `metadata`, `storage_metadata`, `storage_pool` (`pipeline.py:253`) |
| Retardo de descubrimiento (token creado → nosotros lo vemos) | **No existe** | — |
| discovered → `FeaturesComputed` (cruzando contextos) | **No existe** | `analysis_seconds` termina al emitir `TokenDiscovered`, no cuando las features están listas |

**Los dos huecos son los que importan**, y conviene ser preciso sobre por qué:

- **El reloj arranca tarde.** `_process()` empieza a contar cuando el candidato **sale de la cola**
  del pipeline (`pipeline.py:134`). No incluye el intervalo de poll, ni el tiempo en cola bajo
  backpressure. Optimizar lo que esta métrica mide no toca la parte más lenta del recorrido.
- **El reloj termina temprano.** La última etapa es "events" — emitir `TokenDiscovered`. Lo que pasa
  después (features → security → intelligence → committee) no está cronometrado de punta a punta por
  ningún reloj compartido.

`RawTokenCandidate` **sí lleva `created_at`** (`sources/base.py:64`), así que el retardo de
descubrimiento es computable con los datos que ya tenemos — simplemente nadie lo computa. Ese es el
arreglo más barato de esta lista y el prerequisito honesto del punto 2.3: sin él, un "antes y
después" mediría el tramo que menos pesa.

---

## 4. Conclusiones para los puntos 2.2 y 2.3

1. **Es polling, de punta a punta, en los dos caminos** (descubrimiento y precios). No hay push.
2. **El piso de latencia de descubrimiento es ~2.5 s de media** sólo por el intervalo de poll, más lo
   que tarde el proveedor en exponer el token.
3. **El precio puede tener 15 s de antigüedad** cuando se evalúa una salida.
4. **No hay ninguna fuente de profundidad de liquidez ni de flujo de órdenes.** Lo que hay es un
   precio agregado ya digerido.
5. **El seam para una fuente por websocket ya existe** y está documentado en el propio código: el
   puerto `TokenSource` pide sólo `name` + `stream()`. Añadir una fuente streaming es aditivo y no
   toca el Scanner — exactamente la restricción que pide el checklist.
6. **Antes de medir el "antes y después" hay que arreglar el reloj**: hoy empieza al salir de la cola
   y termina al emitir `TokenDiscovered`. Medir con ese reloj daría una mejora aparente sin que nada
   real haya cambiado.

### Lo que queda para tu decisión

Elegir proveedor de streaming implica, en la mayoría de los casos, un plan pago:

- **Helius** (websocket / LaserStream) — ya usamos su RPC, la key está en `.env`.
- **Birdeye** — websocket de trades y precios; plan pago para el tier con order flow.
- **pump.fun** — websocket propio, cubre sólo su propio universo de tokens.

Cuál de estos, y en qué tier, es una decisión de gasto y por tanto tuya. Lo que sí puedo hacer sin
esperar respuesta es el punto 6 de arriba —arreglar el reloj— porque no depende del proveedor y
porque sin él la medición que pide el checklist no significaría nada.
