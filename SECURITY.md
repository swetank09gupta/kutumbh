# Security Policy

Kutumb handles sensitive personal financial data. Security reports are taken
seriously.

## Reporting a vulnerability

**Do not open a public GitHub issue for security problems.**

Instead, please email the maintainer privately:

- **Contact:** swetank.g@greyorange.com
- **Subject line:** `[SECURITY] Kutumb — <one-line summary>`

Include in your report:

1. A description of the issue and its impact
2. Steps to reproduce (ideally a minimal proof of concept)
3. The affected version / commit SHA
4. Any suggested mitigation

You can expect:

- An acknowledgement within **3 business days**
- An initial assessment within **7 business days**
- A fix or mitigation plan communicated back to you before any public disclosure

## Scope

In scope:

- Code in this repository (`app/`, `scripts/`, `alembic/`, `grafana/`)
- The default `docker-compose.yml` configuration
- Documented setup paths in `SETUP.md`

Out of scope:

- Vulnerabilities in third-party dependencies — please report those upstream.
  Dependabot is configured to surface and patch these automatically; see
  [.github/dependabot.yml](.github/dependabot.yml).
- Misconfigurations in a user's own deployment (exposed ports, weak
  `.env` passwords, public Postgres, etc.) — these are the deployer's
  responsibility. See `SETUP.md` for hardening notes.
- Social-engineering, phishing, or physical attacks on a user's machine.
- Issues that require an attacker to already have full local access to the
  host running Kutumb.

## Supported versions

Only the `master` branch receives security fixes. There are no LTS branches.

## Coordinated disclosure

If you'd like credit in the fix commit / release notes, mention so in your
report. The default is to credit reporters unless they ask to remain anonymous.

## Hall of fame

_No reports yet — be the first._
