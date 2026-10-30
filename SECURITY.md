# Security reporting and deployment

Please report suspected vulnerabilities through the maintainer's GitHub profile contact rather than posting credentials or member data in a public issue. Include the affected route, reproduction using synthetic data, and expected authorization behavior.

Use an operator-selected bootstrap password, keep config.json and data/ private, and serve public deployments through HTTPS. Enterprise WeChat callback domains and SMS credentials belong to each operator. Back up SQLite consistently before upgrades. Server-side company and role checks must remain authoritative.

This repository contains no deployed credentials or user database. The automated suite uses synthetic users and mocks provider calls; successful local tests do not establish the configuration of an external enterprise tenant.
