# Capacity sizing (lite)

Last verified: 2026-10-17. Distilled from SolsBrain chapter 14. These are starting points. Say so in the report and recommend measuring before committing.

## Container starting sizes
| Runtime | Idle memory | Main risk | Starting size |
|---|---|---|---|
| Go, Rust | Very small | Goroutine or allocation leaks | 0.25 vCPU, 0.5 GB |
| Python (FastAPI) | Small | Large in-memory buffers, unclosed DB sessions | 0.5 vCPU, 1 GB |
| Node.js (Express, Fastify) | Small | Event listener leaks | 0.5 vCPU, 1 GB |
| Node.js (Next.js server rendering) | Medium | Cached render payloads | 1 vCPU, 2 GB |
| Java (Spring Boot) | Large | Off-heap memory, GC pauses | 2 vCPU, 4 GB |

## Rules to apply
- Container memory limit should be at least 1.25 times the peak measured memory, or the container loops in OOM restarts.
- JVM containers need `-XX:MaxRAMPercentage=75.0` or an explicit `-Xmx`.
- Redis `maxmemory` must stay below the node's RAM (about 75%) to leave room for snapshot copy-on-write. Use `noeviction` only when Redis is the primary store.
- PostgreSQL: keep `work_mem` modest and use a connection pooler (RDS Proxy or PgBouncer) before allowing more than a couple of hundred connections.
- Autoscale workers on queue depth, APIs on CPU or request count.

## Reporting
State the starting size per service, the reason, and "measure under load before production". Do not quote prices or exact durations.
