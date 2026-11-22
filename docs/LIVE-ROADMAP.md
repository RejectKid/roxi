# Roxi live data roadmap

Roxi will expand from reviewing one recorded exercise to answering questions about multiple, continuously updated sources. The initial deployment target is a consented civilian logistics or facilities pilot. Replay and after-action review use fictional drills or authorized non-combat records. The first slice is a warehouse chat room whose messages become searchable, cited proposals while the conversation is happening.

## Milestones

| # | Build | Completion check |
| --- | --- | --- |
| **9. Live chat and replay** | Ingest messages from one authorized civilian logistics room through a webhook, with an NDJSON replay adapter for development. Store originals, normalize message and edit events, and stream new proposals into the timeline and review queue. Classify reports, acknowledgments, corrections, and ordinary chatter. | A replay with duplicates, edits, and late delivery produces one stored original per source version. Stop and resume the worker without losing acknowledged input. On a named pilot workstation, sustain 10 messages per second for 15 minutes with p95 under 3 seconds from receipt to visible proposal. |
| **10. More source types** | Add a versioned adapter contract and source catalog. Ingest a CSV asset and site register, one civilian transit or synthetic fleet feed, and one environmental or public weather feed. Display last successful fetch, schema version, source latency, and coverage gaps. | The same ingestion and evidence contract handles chat, files, and external observations. A bad payload becomes inspectable, an unavailable feed appears stale, and replay after a parser update creates auditable derived versions. |
| **11. Time, identity, and maps** | Add an entity registry for facilities and civilian assets. Link source-specific IDs through reviewed aliases. Show recent reports, sensor observations, and versioned geographic layers together on a map and timeline. Expose `as_of` and `known_at` filters. | Replaying an earlier view gives the same result. A late observation appears after its receipt time and never silently rewrites what users could see earlier. Ambiguous identifiers remain unresolved and conflicting observations stay visible. |
| **12. An assistant and developer interfaces** | Add source-backed answers for “what changed?”, “what is overdue?”, and “which updates disagree?”. Add a typed query model, OpenAPI examples and SDK, plus the same read operations over MCP. Include a change feed with resume tokens for application integrations. Prototype push-to-talk questions and spoken briefings after the text path passes evaluation. | UI, HTTP, and MCP return the same claims and evidence for a pinned view. A reconnecting client resumes without gaps. Every supported claim opens exact authorized evidence. Missing context, invalid citations, and planted misleading content are exercised in the evaluation. |
| **13. Pilot access and operations** | Add authenticated workspaces, separate ingestion, review, and reading permissions, source-level access, retention and deletion tooling, and operational dashboards. Document deployment, backups, restore, and connector health. | Access revocation takes effect for source links, snapshots, API calls, and MCP. A restore exercise recovers the data and checkpoints. A disconnected or backlogged source is obvious to operators. Finish these checks before collecting real participant messages. |
| **14. Civilian pilot and evaluation** | Run a small, authorized receiving-dock or facilities pilot. Compare the live assistant and structured timeline with the user's existing process using the same categories and equivalent questions. | Measure time to a supported answer, missed corrections, false links, review load, and citation accuracy. Publish performance and failure cases, and use the results to choose the next connectors and interface changes. |

The initial speed and load numbers are engineering targets. Measure them on a named machine and revise them once the source mix and pilot workload are known.

## Architecture and data contract

```mermaid
flowchart LR
  C[Chat, files, and civilian feeds] --> I[Source adapters]
  I --> R[Original bytes and append-only events]
  R --> P[Normalization and classification]
  P --> V[Live proposals and human review]
  V --> S[Published snapshots]
  R --> E[Evidence resolution]
  S --> Q[Shared query service]
  E --> Q
  Q --> A[Assistant and timeline]
  Q --> D[HTTP and MCP]
  S --> F[Resumable change feed]
```

- **Source event.** Record a workspace, source, source event key and revision, payload kind and schema, event time when known, receipt time, and original evidence reference. Store connector provenance and raw digests. Scope deduplication to the workspace and source. Model edits and deletions as new source versions so replay explains what changed while access and retention still apply. Start from CloudEvents field conventions and map them into Roxi's internal schema. [CloudEvents specification](https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md)
- **Derived record.** Store a type, normalized fields, classification method and version, evidence locators, review state, and links such as `corrects` and `acknowledges`. Keep uncertain classifications in the queue. Keep original text and observed values distinguishable from reviewer decisions. W3C PROV provides a vocabulary for describing where a record came from and what produced it. [W3C PROV overview](https://www.w3.org/TR/prov-overview/)
- **Current and historical views.** Mark new proposals visibly and require explicit review for published snapshots. Let users pin an observed-time cutoff and a receipt-time cutoff. Preserve distinct values when sources disagree. Each view shows which sources are included, whether they are fresh, and which readings or timestamps are missing.
- **Storage and queues.** Keep PostgreSQL for normalized data, publication, and a durable processing queue. Continue archiving original bytes by digest. Add per-connector checkpoints, bounded retries, backlog limits, and a quarantine for malformed events. Introduce external object storage or a broker when measured storage, throughput, or consumer isolation warrants it. NiFi provides useful examples of lineage, material replay, and backpressure. [Apache NiFi user guide](https://nifi.apache.org/nifi-docs/user-guide.html)
- **Evidence locators.** Support message ID and version, source row or JSON pointer, audio interval, and geospatial feature ID with its source layer version. Resolve each locator through the same access policy as the original. Include `supported`, `conflicting`, or `insufficient_evidence` states at the claim level. Test whether claims remain supportable as sources change or become unavailable. [NIST Generative AI Profile](https://nvlpubs.nist.gov/nistpubs/AI/NIST.AI.600-1.pdf)

## Source priorities

| Priority | Source | Pilot use |
| --- | --- | --- |
| First | Authorized chat and NDJSON replay | Categorize dock reports, corrections, acknowledgments, and ordinary discussion as messages arrive. |
| First | CSV and JSON uploads | Load a facility and equipment register plus maintenance events. Keep source row numbers and import diagnostics. |
| Next | Public transit or synthetic civilian fleet | Use scheduled updates and public vehicle positions to validate late records, identity mapping, or missing updates. [GTFS Realtime reference](https://gtfs.org/documentation/realtime/reference/) |
| Next | Environmental observations and public weather | Attach building or weather observations to site records while preserving their units and coverage. [OGC SensorThings Sensing](https://www.ogc.org/standards/sensorthings/) · [National Weather Service API](https://www.weather.gov/documentation/services-web-api) |
| Next | Civilian facility maps and public elevation | Add facility features and elevation when a defined accessibility or facilities question requires them. Record layer versions, coordinate reference systems, and applicable attribution. [OGC API Features](https://www.ogc.org/standards/ogcapi-features/) · [USGS 3D Elevation Program](https://www.usgs.gov/3d-elevation-program) |
| Later | Consented recordings and transcripts | Ingest chunked transcripts with provisional and final states. Keep original offsets and display transcript coverage. Benchmark recognition on varied audio before using it in the pilot. |

## How people and applications use it

- **Assistant.** Support questions such as “What changed at the receiving dock since the last shift?” and “Which facilities updates disagree?” Answer with a time window, the sources checked, claim-by-claim citations, and explicit coverage gaps. Add saved questions and reviewable shift summaries after the single-question flow works.
- **HTTP and SDK.** Use OpenAPI for stable typed queries, pagination, versioned results, and client generation. Offer bounded operations for timeline search, entity history, snapshot comparison, and evidence resolution. Keep aggregation and temporal checks in the service so callers receive reproducible results. [OpenAPI specification](https://spec.openapis.org/oas/latest.html)
- **MCP.** Reuse the same query service for assistant and agent reads. Expose feature and event schemas as resources and use structured results for evidence and coverage. Keep stdio for local use and evaluate authenticated remote MCP as part of the pilot permission work. [Model Context Protocol architecture](https://modelcontextprotocol.io/docs/learn/architecture) · [MCP security practices](https://modelcontextprotocol.io/specification/latest/basic/security_best_practices)
- **Change feed.** Provide resumable server-sent events for browser updates and a versioned subscription contract for system consumers. Publish content identifiers, revision stamps, and event types. Managed integrations fetch details under their own authorization. Document schemas and delivery guarantees with AsyncAPI when there is a second consumer. [Server-sent events](https://developer.mozilla.org/en-US/docs/Web/API/Server-sent_events/Using_server-sent_events) · [AsyncAPI specification](https://www.asyncapi.com/docs/reference/specification/v3.1.0)
- **Operator view.** Show connector health, last received event, queued or failed work, and per-class review outcomes. Record throughput, p95 delay, retry rates, and source availability. Keep message text out of routine telemetry. [OpenTelemetry signals](https://opentelemetry.io/docs/concepts/signals/)

## First implementation tickets

1. Add a synthetic chat replay fixture with deterministic delivery times, edits, duplicates, and disconnects. Include an answer key for which messages should be classified and why.
2. Define the source event and locator schemas. Implement idempotent capture and checkpointing for one authorized webhook.
3. Reuse the existing glossary and correction rules to produce proposals. Add one live timeline stream and retain manual review and publication.
4. Run restart, backlog, duplicate, stale-source, and evidence-access checks. Record latency and classification results before choosing the second connector.
