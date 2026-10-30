# iTrial OKR architecture decision

## Context

Standalone extraction of the existing OKR module for small enterprise teams: company/department/personal goals, approvals, monthly assessment and exports. One FastAPI process owns one SQLite database. No CRM, DevOps, QMS, knowledge base or other platform data is included.

## Chosen architecture

Use one modular FastAPI application and plain static HTML/CSS/JavaScript. This is a relocation, not a business rewrite. Existing SQL orchestration and long handlers are explicitly recorded as legacy exceptions; new use cases must move orchestration into services. No new infrastructure is needed.

## Business modules

The extracted OKR package is the only business module.

| Module | Responsibility | Public surface | Owned data | Allowed dependencies | Owner |
|---|---|---|---|---|---|
| okr | Goals, approval, identity, assessment, reports | okr/public.py; /api/okr/* | data/okr.db | Python stdlib and requirements.txt | huevan |

## Dependency direction

```text
entrypoint/delivery -> application use cases -> domain rules
                                           -> ports <- infrastructure adapters
```

The entrypoint imports okr.public; package modules use relative imports. Legacy root modules alias the same implementation, preserving monkeypatch and import identity. Frontend pages use okr-common.js for authenticated API transport and feature-local rendering scripts. There are no cross-business imports.

## Contracts and compatibility

All existing /api/okr/* request/response contracts and /okr page paths remain. Server-side Pydantic validation and HTTPException detail errors remain authoritative. Model qualified names retain okr_models for compatibility with the integrated platform schema. Tests cover original consumers and package aliases.

## Persistence and transactions

okr/db.py owns SQLite schema and existing idempotent startup migrations, WAL, timeout and context-managed transactions. This extraction adds no schema migration. Back up the SQLite database consistently before upgrades. Roll code back without replacing live business data.

## Authentication, authorization, and data isolation

Enterprise WeChat OAuth creates accounts; first-use SMS verifies phone ownership. Company and role checks remain on the server, with audit records. Password login is reserved for the bootstrap super administrator. Standalone setup uses an operator-chosen password and random JWT secret. No deployed credentials or operational member database ship. Maintainer-provided screenshots are separately identified as partially masked views of an existing deployment. The public extraction replaces the legacy fixed seed credentials only; existing integrated deployment seeding is unchanged.

## External integrations and jobs

okr/wecom.py and okr/sms.py own vendor HTTP calls, signatures and bounded timeouts. Provider calls are mocked in tests. No SMS or paid API is invoked during verification. PyMuPDF owns PDF generation and its producer metadata is preserved. D3, ECharts and SheetJS remain externally loaded browser dependencies.

## Quality gates

The commands in `.engineering/project.yaml` are required locally and in CI. Architecture exceptions must be narrow, owned, tested, and temporary.

## Deployment and rollback

Local Python or Docker Compose serves port 8888. Health is GET /healthz; smoke tests cover pages, unauthorized API access and an authenticated read in isolated test data. Docker persists data/ and mounts ignored config.json read-only. Public GitHub publication is authorized by the user; this does not authorize deploying any remote service. Publish only the audited standalone tree and never the parent repository history.
