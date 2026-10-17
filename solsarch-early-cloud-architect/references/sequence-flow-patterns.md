# Sequence flow patterns

Pick the pattern that matches the scan, then rename participants to the repo's own services.
Avoid semicolons in message text (the sanitizer escapes them, but plain text reads better).

## A. Authentication with an identity provider (OIDC and JWT)

```mermaid
sequenceDiagram
  autonumber
  participant C as Client
  participant W as WAF and CDN
  participant L as Load balancer
  participant A as API
  participant I as Identity provider
  C->>W: GET /login
  W->>L: Forward request
  L->>A: Forward request
  A-->>C: Redirect to identity provider
  C->>I: Sign in
  I-->>C: Authorization code
  C->>A: Callback with code
  A->>I: Exchange code for tokens
  I-->>A: ID token and access token
  A-->>C: Session cookie
  C->>A: API call with token
  A->>A: Validate signature, expiry, audience
  A-->>C: 200 response
```

Include when the scan shows an auth library (passport, next-auth, authlib, jsonwebtoken, PyJWT) or the user mentions login. Name the real identity provider if known.

## B. Streaming RAG / LLM request

```mermaid
sequenceDiagram
  autonumber
  participant U as Browser
  participant L as Load balancer
  participant A as API
  participant V as Vector store
  participant M as Model API
  U->>L: POST /chat (question)
  L->>A: Forward, idle timeout 300s or more
  A->>V: Similarity search on question embedding
  V-->>A: Top matching chunks
  A->>M: Prompt with chunks, stream enabled
  loop Until the model finishes
    M-->>A: Token chunk
    A-->>U: Server-sent event
  end
  A->>A: Save conversation and usage
  A-->>U: Stream complete
```

Include when the scan shows an AI SDK plus a vector store. This is the diagram that SEC-004 protects: label the load balancer timeout.

## C. Asynchronous job

```mermaid
sequenceDiagram
  autonumber
  participant U as User
  participant A as API
  participant Q as Queue
  participant W as Worker
  participant D as Database
  participant S as Object storage
  U->>A: Upload document
  A->>S: Store file
  A->>D: Create job (status queued)
  A->>Q: Enqueue job id
  A-->>U: 202 Accepted with job id
  Q->>W: Deliver job
  W->>S: Read file
  W->>D: Save results, status done
  U->>A: Poll job status
  A->>D: Read status
  A-->>U: Done
```

Include when the scan shows Celery, BullMQ, RQ, Sidekiq or a queue image. Mention retries and a dead-letter queue if the design adds them.

## D. Plain CRUD request (default when none of the above apply)

```mermaid
sequenceDiagram
  participant C as Client
  participant L as Load balancer
  participant A as API
  participant D as Database
  C->>L: HTTPS request
  L->>A: Forward
  A->>D: Query
  D-->>A: Rows
  A-->>C: JSON response
```

## Rules

- Draw two sequences per design: authentication (A or "no auth, public API" noted in text) and the app's core data path (B, C or D).
- Show the trust boundary: the first arrow after the internet must hit WAF, CDN or load balancer.
- Databases never start a call. Arrows from them are replies only.
- Label timeouts and protocols where they matter (SEC-004).
