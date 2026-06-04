# Notices, Trademarks, and Attributions

Kutumb is an independent, open-source project. It is **not affiliated with,
endorsed by, or sponsored by** any of the institutions or services listed below.
All trademarks, service marks, trade names, and logos are the property of their
respective owners and are referenced solely to describe interoperability.

## Financial institutions referenced

The following names appear in this project as parser targets, dashboard labels,
or documentation. Each is a registered trademark of its owner:

- **HDFC Bank®** — HDFC Bank Limited
- **State Bank of India® (SBI)** — State Bank of India
- **Axis Bank®** — Axis Bank Limited
- **Union Bank of India®** — Union Bank of India
- **Zerodha®, Kite®, Coin®, Console®** — Zerodha Broking Limited
- **CAMS®** — Computer Age Management Services Limited
- **KFintech®** — KFin Technologies Limited
- **CDSL®** — Central Depository Services (India) Limited
- **ICICI®** (referenced in sample data only) — ICICI Bank Limited

## Data sources

- **AMFI NAVAll.txt** — Association of Mutual Funds in India.
  Used for scheme NAV lookup and ISIN resolution. Free to use; we cache the file
  locally for 24 hours to minimise load on AMFI infrastructure.
  Source: <https://www.amfiindia.com/spages/NAVAll.txt>

- **NSE Indices (Nifty 50, Nifty Midcap 150, Nifty Smallcap 250 P/E)** —
  National Stock Exchange of India Limited. Index values are fetched from
  publicly accessible endpoints for personal, non-commercial use. NSE indices
  are proprietary to NSE; commercial redistribution requires a licence from NSE
  Indices Limited.

- **RBI USD/INR reference rate** — Reserve Bank of India.
  Public-domain reference data.

## Third-party software

Kutumb depends on numerous open-source libraries. A summary of their licences
is available in [THIRD_PARTY_LICENSES.md](THIRD_PARTY_LICENSES.md). All
dependencies are distributed under permissive licences (MIT, BSD, Apache-2.0,
PSF). No copyleft (GPL/AGPL/LGPL) dependencies are bundled.

Notable runtime components:

- **Grafana®** — Grafana Labs. Distributed under AGPL-3.0 in its own container;
  Kutumb interacts with it over a network boundary (HTTP) and does not link or
  bundle Grafana source. The pre-built dashboard JSON in `grafana/dashboards/`
  is authored by the Kutumb project and licensed under the project's MIT.
- **PostgreSQL®** — The PostgreSQL Global Development Group. PostgreSQL License.
- **Ollama** — Ollama. MIT License. Models pulled (e.g. `llama3.2`) are subject
  to their own licences from the model authors (Meta Llama Community License
  for Llama 3.2).
- **Docker®, Docker Compose®** — Docker, Inc.

## Reporting

If you are a rights holder and believe Kutumb references your trademark or data
in a manner that requires correction or removal, please open an issue on the
project repository.
