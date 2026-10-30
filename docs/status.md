# Current delivery status

Updated: 2026-10-30

## Core feature screenshots

The maintainer explicitly requested publication of five supplied PNG screenshots, including a subsequent personal objective/KR editor view. They are copied byte-for-byte into `docs/screenshots/` and embedded in the root README: target alignment/mind map, personal objective/KR editing, statistics, monthly member evaluation, and administrative user management. Existing masking is retained; the README identifies them as partially masked screenshots of an existing deployment rather than bundled application data.

The monthly evaluation screenshot contains historical objective-score wording. Its caption states the current self-assessment 20% / manager 80% rule and that KR progress is display-only. No screenshot is presented as evidence of the current scoring implementation.

Verification for this documentation-only update: PNG signatures/dimensions and absence of embedded text/EXIF metadata checked; source and copied SHA-256 hashes match. README links, static quality gates, publish scan and final GitHub file hashes are checked for publication. Business code, configuration and data remain unchanged; no application deployment is required.

## Standalone source extraction baseline

The OKR-only source tree has been extracted from the integrated platform. Existing business handlers, request contracts, company isolation and assessment rules are retained. Root okr_*.py files are compatibility aliases. Standalone initialization replaces legacy fixed bootstrap credentials with operator input and a random JWT secret.

Repository: https://github.com/huevan/itrial-okr . License: AGPL-3.0.

Verified locally:

- Fresh Python 3.13 virtual environment: all 76 tests pass with the pinned dependency set, and pip check reports no broken requirements.
- Architecture guard, publish scan and staged whitespace checks pass. The export also compared the allowlisted source against the original configuration's secret values without logging them.
- Docker image builds successfully; isolated container initialization, health, three pages, static assets, unauthorized access and authenticated reads work. The container exposes no other business module or configuration file.
- All 56 existing OKR page/API route contracts match the integrated platform after resolving schema references. The integrated platform retains its complete original OpenAPI document, all 12 HTTP status baselines and unchanged OKR table row counts across read verification. Its 74 OKR tests and six ZYTB regressions pass.

Not verified: live Enterprise WeChat OAuth, real SMS delivery, a public server deployment, browser interaction with real member records, or the complete parent platform's test suite. CI is configured for Python 3.12/3.13; hosted job results are separate evidence. No remote service deployment is part of this delivery.
