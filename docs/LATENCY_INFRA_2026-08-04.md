# Infraestructura de latencia — informe, no aprovisionamiento

**Fecha:** 2026-08-04 · **Rama:** `claude/hades-bot-wiring-issues-8bcbnj`
**Alcance:** checklist §6 — comparar opciones (RPC dedicado, colocation cerca de validators de Solana,
proveedores) con costos y tradeoffs de latencia esperada.

**No se contrató ni se aprovisionó nada.** Este documento existe para que la decisión se tome con
números delante. Lee también `docs/EXECUTION_FAST_PATH_2026-08-04.md` (§1) y
`docs/SCANNER_INGEST_AUDIT_2026-08-04.md` (§2): las tres piezas de latencia son el mismo problema
visto desde tres lugares, y **el orden en que conviene atacarlas no es el orden en que cuestan**.

---

## 1. Dónde está la latencia hoy, medida contra lo que sabemos

Antes de comparar proveedores, el presupuesto de latencia real de Hades, con lo que está verificado
y lo que no:

| Tramo | Magnitud | ¿Medido? | ¿Lo arregla un RPC mejor? |
|---|---|---|---|
| Fuente upstream expone el token → nuestro poll lo ve | **~2,5 s de media** (intervalo de 5 s) | Piso deducible del código; el retardo total ahora se mide (§2.3, `hades_scanner_discovery_lag_seconds`) | **No.** Es polling HTTP contra DexScreener/pump.fun/Raydium, no RPC |
| discovered → features listas | Ahora medido (`hades_scanner_discovered_to_features_seconds`) | Sí, desde este commit | **No.** Es CPU y I/O local |
| Precio para evaluar una salida | **Hasta 15 s de antigüedad** (caché) | Deducible del código | **No.** Es la caché de DexScreener |
| Cotización del swap | No medido | No (no hay `QuoteProvider`) | Parcialmente |
| Firma → landing on-chain | **No medido, y hoy no ejecutable** | No | **Sí, es exactamente esto** |

**La conclusión incómoda va primero:** de los cinco tramos, **un RPC dedicado o colocation sólo toca
el último** — y ese último es el único que hoy **no existe** (no hay adaptadores live, §1). Los tres
primeros, que son los que realmente están frenando la plataforma hoy, no mejoran ni un milisegundo
por gastar en infraestructura de RPC.

Gastar \$2.900/mes en un nodo dedicado para arreglar el tramo que no está construido, mientras el
descubrimiento arrastra 2,5 s de polling y los precios 15 s de caché, sería la peor relación
coste/beneficio disponible.

---

## 2. El punto de partida

| | |
|---|---|
| Host | CT 203, LXC en Proxmox — **2 vCPU, 3 GB RAM, sin swap** |
| Ubicación | Homelab, conexión doméstica |
| Proveedores RPC configurados | **Helius** y **QuickNode** (con failover por health-score en `RpcManager`) |
| Uso actual | Sólo lectura: el Scanner y el Security Engine. Nada firma ni envía |

Dos cosas que condicionan todo lo que sigue:

1. **La caja tiene 2 vCPU y todo el pipeline corre en un solo proceso (`worker`).** La auditoría de
   julio lo marcó como B1/D6 y sigue abierto. Un RPC más rápido entrega respuestas antes a un
   proceso que ya compite consigo mismo.
2. **La conexión es doméstica.** La latencia de red hasta cualquier proveedor está dominada por el
   último tramo, que no se contrata. Colocation implica **mover el bot**, no mejorar el enlace.

---

## 3. Opciones, con precios

### O1 — Seguir como estamos (Helius + QuickNode, tiers actuales)

| | |
|---|---|
| Coste | Lo que ya se paga. Helius: gratis 1M créditos / 10 RPS · Developer \$49/mes (10M) · Business \$499/mes (100M). QuickNode arranca en \$49/mes |
| Latencia | Compartida, sin garantías |
| Tradeoff | Ninguno nuevo. Es el baseline contra el que hay que medir |

### O2 — RPC dedicado / staked

| | |
|---|---|
| Coste | **Triton: desde ~\$2.900/mes** por nodo dedicado (enfoque enterprise), más bandwidth. Por volumen, ~\$10/M llamadas (RPC estándar) o ~\$25/M (ledger histórico) |
| Latencia | Los nodos staked de Triton con Yellowstone gRPC están entre las latencias más bajas documentadas del ecosistema, **~100 ms** |
| Tradeoff | Es el salto de precio más grande de la lista, y **sólo toca el tramo que hoy no existe** |
| Decisión tuya | **Sí** — es contratar RPC pago/dedicado, en la lista explícita |

### O3 — Colocation cerca de validators

| | |
|---|---|
| Coste | Variable, pero por encima de O2: hay que sumar el hosting del bot |
| Latencia | Para *landing* de transacciones desde Europa, los proveedores colocados en Fráncfort tienen una ventaja física de **80–150 ms** sobre los que sólo están en EE. UU. |
| Tradeoff | **Implica mover Hades fuera del homelab.** Se pierde el control físico, cambia el modelo de backups, de secretos y de acceso, y el CT 203 deja de ser el sistema de referencia |
| Decisión tuya | **Sí**, y es la más pesada: no es una factura, es un cambio de arquitectura operativa |

### O4 — Sacar el pipeline del proceso único (no cuesta dinero)

No es una opción de infraestructura, y por eso mismo merece estar en esta lista.

| | |
|---|---|
| Coste | **\$0.** El proceso `engine` ya existe, supervisado y vacío, exactamente para esto (`ops/engine.py:26`) |
| Latencia | No medida. La auditoría la marca como estructural (B1/R4), no perfilada |
| Tradeoff | Trabajo de ingeniería, y la auditoría advierte de no separar procesos **sin evidencia de contención real** — el mismo criterio que el checklist §7 aplica al event bus |
| Decisión tuya | No. Pero sí requiere medir antes |

---

## 4. Recomendación

**No contratar nada todavía, y no porque sea caro: porque hoy no habría cómo saber si sirvió.**

El orden que propongo:

1. **Ya hecho (§2.3):** los dos relojes de ingesta. Sin ellos, cualquier "mejoró" era indemostrable.
2. **Medir antes de gastar.** Con `discovery_lag_seconds` y `discovered_to_features_seconds` corriendo
   unos días en el CT, sabremos qué fracción del retardo es del proveedor y cuál nuestra. Eso es
   gratis y responde la pregunta que decide entre O1 y O2.
3. **Bajar el intervalo de poll antes que cambiar de proveedor.** Los ~2,5 s de media del polling son
   el tramo más grande y barato de atacar, y no cuesta un centavo — sólo rate limit. Es, con
   diferencia, la mejor relación coste/latencia disponible hoy.
4. **Correr §7 (estrés del pipeline).** Si el techo aparece en el event bus o en el worker único, O4
   (gratis) vale más que O2 (\$2.900/mes).
5. **Recién entonces evaluar O2**, y sólo si además se decidió construir los adaptadores live — sin
   ellos no hay nada que aterrizar y el nodo dedicado no tiene función.
6. **O3 (colocation) queda fuera del horizonte razonable** mientras el bot no ejecute en vivo de forma
   sostenida y rentable en paper. Mover Hades del homelab por 80–150 ms de un tramo que no existe
   sería invertir el orden de todo lo demás.

### Lo que necesito de vos, cuando toque

- Si se construyen o no los adaptadores live (decidido hoy: **congelado**, §1).
- Si autorizás bajar `SCANNER_POLL_INTERVAL_SECONDS` — es gratis, pero cambia el consumo de rate
  limit contra los proveedores actuales, así que no lo toco sin avisarte.
- Cualquier alta de plan pago (O2), que es la lista explícita de "no decido yo".

---

## Fuentes

- [The Complete Guide to Solana RPC Providers in 2026 — Sanctum](https://sanctum.so/blog/complete-guide-solana-rpc-providers-2026)
- [Solana RPC Providers — 2026 Comparison for Traders, MEV Searchers & Builders — AllenHark](https://allenhark.com/solana-rpc-providers-comparison)
- [Best Solana RPC Providers for MEV in 2026 — Dysnix](https://dysnix.com/blog/solana-rpc-for-mev)
- [9 Best Solana RPC Providers (2026): Decision Guide — Alchemy](https://www.alchemy.com/overviews/solana-rpc)
- [Best Solana RPC Providers (2026): A Full Comparison — QuickNode](https://blog.quicknode.com/best-solana-rpc-providers-2026/)
