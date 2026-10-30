# Project engineering contract

This repository inherits the global engineering instructions. These project rules are mandatory for generated and hand-written code.

## Before feature code

- Read `.engineering/project.yaml` and `.engineering/architecture.md`.
- Resolve every `REQUIRED_DECISION`; do not generate business code while architecture decisions are incomplete.
- Run `python scripts/architecture_guard.py` before and after structural changes.
- Record the owning business module, public contract, data ownership, permitted dependencies, and verification commands for every new capability.

## Module boundaries

- Entrypoints contain startup, routing, middleware, lifecycle, and dependency wiring only.
- Backend delivery code calls application use cases; repositories and gateways own persistence and external transport.
- Frontend app-shell code owns routing, session, providers, and error boundaries only. Business pages, state, API clients, types, validation, styles, and tests belong to feature modules.
- Modules may use another module only through its documented public surface. Cross-module internal imports and circular dependencies are prohibited.
- Keep behavior local until at least two real consumers justify a stable, business-independent shared abstraction.
- Treat `shared`, `common`, base components, core clients, public contracts, and global styles as high-impact code. Identify all consumers before changing them.

## Change gates

- Boundary gate: state the owning module and dependency direction before implementation.
- Contract gate: locate and update all producers and consumers for API, event, schema, or shared-type changes.
- Persistence gate: define transaction, concurrency, migration, compatibility, and rollback behavior for data changes.
- Quality gate: run formatting, linting, type checking, focused tests, full tests, production build, and architecture guard as declared in `.engineering/project.yaml`.
- Maintainability gate: reject mixed responsibilities, duplicated business logic, hidden mutable global state, direct vendor calls outside gateways, direct SQL in controllers, and raw network transport in frontend pages.

## Exceptions

Architecture-budget exceptions must be narrow and recorded in `.engineering/architecture-guard.json` with a reason, owner, regression evidence, and removal condition. Do not weaken or delete a gate merely to make a build pass.

## Delivery

Deployment is separate from code completion and requires explicit authorization for the named environment. Completion reports list changed files, module ownership, contracts, checks actually run, skipped checks, remaining risk, and deployment status.
