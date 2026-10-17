# Diagram standards

Four diagrams, all Mermaid, all in one markdown file. After writing, run
`python scripts/diagram_sanitizer.py --markdown FILE --in-place` and fix anything it reports.

## Rules

- Quote every node label: `api["API (FastAPI)"]`. Never leave parentheses, slashes or colons unquoted.
- Node ids use letters, digits and underscores only. Do not name a node `end`.
- One idea per diagram. If a diagram passes about 20 nodes, split it.
- Use `<br/>` for line breaks inside a label. Do not use other HTML.
- Name nodes by what the repo calls them (service, folder, package), then the cloud service in brackets.
- Every box in diagram 1 and 2 that is a cloud service must be a row in the decision table.
- Add a one-line caption under each diagram saying what it shows.
- Default to flowcharts for the C4 levels. Native C4 blocks in Mermaid are experimental and many viewers (GitHub included) do not render them. Use the native blocks below only when the user's viewer supports them.

## Colour classes

Take hex values from `assets/cloud-vendor-palette.json`. A class only colours a diagram if its `classDef` line is in the same Mermaid block, so paste the lines you use at the bottom of every flowchart:

```text
classDef aws fill:#FF9900,stroke:#232F3E,color:#232F3E
classDef azure fill:#0078D4,stroke:#003A6C,color:#FFFFFF
classDef gcp fill:#4285F4,stroke:#174EA6,color:#FFFFFF
classDef data fill:#E8F0FE,stroke:#3C4043,color:#202124
classDef user fill:#FFFFFF,stroke:#5F6368,color:#202124
classDef risk fill:#FDE7E9,stroke:#A3262A,color:#A3262A
```

Use `:::risk` on any node tied to an open blocker so the diagram shows it.

## 1. Cloud infrastructure

```mermaid
flowchart TB
  user["Users"]:::user
  subgraph edge["Edge"]
    waf["AWS WAF + CloudFront"]:::aws
  end
  subgraph vpc["VPC 10.0.0.0/16"]
    subgraph pub["Public subnets (2 AZs)"]
      alb["Application Load Balancer"]:::aws
      nat["NAT Gateway"]:::aws
    end
    subgraph app["Private app subnets (2 AZs)"]
      api["ECS Fargate: API"]:::aws
      worker["ECS Fargate: queue workers"]:::aws
    end
    subgraph dat["Private data subnets (2 AZs)"]
      db[("Aurora PostgreSQL, multi-AZ")]:::data
      cache[("ElastiCache Redis")]:::data
    end
  end
  user --> waf --> alb --> api
  api --> db
  api --> cache
  worker --> cache
  worker --> db
  api -.->|"HTTPS egress"| nat
  classDef aws fill:#FF9900,stroke:#232F3E,color:#232F3E
  classDef data fill:#E8F0FE,stroke:#3C4043,color:#202124
  classDef user fill:#FFFFFF,stroke:#5F6368,color:#202124
```

Show: zones or subnets, what is public, what is private, the path from user to data, egress.

## 2. C4 model

Context (people and external systems):

```mermaid
C4Context
  title System context
  Person(user, "User", "Uses the web app")
  System(app, "Shop", "Sells products")
  System_Ext(idp, "Identity provider", "OIDC login")
  System_Ext(pay, "Payment API", "Card payments")
  Rel(user, app, "Uses", "HTTPS")
  Rel(app, idp, "Authenticates with", "OIDC")
  Rel(app, pay, "Charges with", "HTTPS")
```

Container (deployable units):

```mermaid
C4Container
  title Containers
  Person(user, "User", "Browser")
  System_Boundary(sys, "Shop") {
    Container(web, "Web app", "Next.js", "Server-rendered UI")
    Container(api, "API", "FastAPI", "Business logic and REST")
    ContainerQueue(q, "Job queue", "SQS", "Background work")
    Container(worker, "Worker", "Celery", "Processes jobs")
    ContainerDb(db, "Database", "Aurora PostgreSQL", "Orders and users")
  }
  Rel(user, web, "Uses", "HTTPS")
  Rel(web, api, "Calls", "HTTPS")
  Rel(api, db, "Reads and writes", "SQL")
  Rel(api, q, "Enqueues", "HTTPS")
  Rel(worker, q, "Consumes")
  Rel(worker, db, "Writes", "SQL")
```

Component (inside one container, only for the container that matters most):

```mermaid
C4Component
  title API components
  Container_Boundary(api, "API") {
    Component(routes, "Route handlers", "FastAPI routers", "HTTP endpoints")
    Component(auth, "Auth middleware", "JWT validation", "Checks bearer tokens")
    Component(svc, "Services", "Python modules", "Business rules")
    Component(repo, "Repositories", "SQLAlchemy", "Data access")
  }
  Rel(routes, auth, "Uses")
  Rel(routes, svc, "Calls")
  Rel(svc, repo, "Calls")
```

Flowchart fallback for any C4 level:

```mermaid
flowchart LR
  user["User (person)"]:::user
  subgraph sys["Shop (system)"]
    web["Web app<br/>Next.js"]
    api["API<br/>FastAPI"]
    db[("Database<br/>Aurora PostgreSQL")]:::data
  end
  user --> web --> api --> db
  classDef data fill:#E8F0FE,stroke:#3C4043,color:#202124
  classDef user fill:#FFFFFF,stroke:#5F6368,color:#202124
```

## 3. Runtime sequences

See `references/sequence-flow-patterns.md`. Two diagrams: authentication, and the core data path.

## 4. Code topology

Map repository folders to the runtime that hosts them, citing the scan.

```mermaid
flowchart LR
  subgraph repo["Repository"]
    fe["web/ (Next.js)"]
    be["api/ (FastAPI)"]
    jobs["workers/ (Celery)"]
    mig["migrations/"]
  end
  subgraph run["Runtime"]
    cdn["CloudFront + S3"]:::aws
    svc["ECS service: api"]:::aws
    wrk["ECS service: workers"]:::aws
    rds[("Aurora PostgreSQL")]:::data
  end
  fe --> cdn
  be --> svc
  jobs --> wrk
  mig -.->|"run at deploy"| rds
  classDef aws fill:#FF9900,stroke:#232F3E,color:#232F3E
  classDef data fill:#E8F0FE,stroke:#3C4043,color:#202124
```

Rules: every top-level folder with code appears once; shared libraries are drawn as one
box with arrows to each user; folders that are not deployed (docs, scripts) are omitted.
