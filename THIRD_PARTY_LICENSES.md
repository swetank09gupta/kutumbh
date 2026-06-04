# Third-Party Licenses

Kutumb bundles or depends on the following third-party packages. All are
distributed under permissive licences (MIT, BSD, Apache-2.0, PSF, or
public-domain-equivalent). No copyleft (GPL / AGPL / LGPL) dependencies are
included in the Python distribution.

Versions reflect `requirements.txt` at the time of generation. Regenerate this
file after dependency changes with:

```bash
pip-licenses --format=markdown --with-urls --order=license \
  --packages fastapi uvicorn sqlalchemy alembic psycopg2-binary pydantic \
  pydantic-settings python-dotenv pyyaml imapclient pdfplumber pandas numpy \
  pyarrow apscheduler httpx requests pycryptodome lxml aiofiles \
  python-dateutil casparser > THIRD_PARTY_LICENSES.md
```

## Python dependencies

| Package            | Version       | License                       | URL |
|--------------------|---------------|-------------------------------|-----|
| fastapi            | 0.115.0       | MIT                           | <https://github.com/fastapi/fastapi> |
| uvicorn            | 0.30.6        | BSD-3-Clause                  | <https://www.uvicorn.org/> |
| sqlalchemy         | 2.0.36        | MIT                           | <https://www.sqlalchemy.org/> |
| alembic            | 1.13.3        | MIT                           | <https://alembic.sqlalchemy.org/> |
| psycopg2-binary    | 2.9.9         | LGPL-3.0-or-later (with OpenSSL exception) | <https://www.psycopg.org/> |
| pydantic           | 2.9.2         | MIT                           | <https://docs.pydantic.dev/> |
| pydantic-settings  | 2.5.2         | MIT                           | <https://github.com/pydantic/pydantic-settings> |
| python-dotenv      | 1.0.1         | BSD-3-Clause                  | <https://github.com/theskumar/python-dotenv> |
| pyyaml             | 6.0.2         | MIT                           | <https://pyyaml.org/> |
| imapclient         | 3.0.1         | BSD-3-Clause                  | <https://github.com/mjs/imapclient> |
| pdfplumber         | 0.11.4        | MIT                           | <https://github.com/jsvine/pdfplumber> |
| pandas             | 2.2.3         | BSD-3-Clause                  | <https://pandas.pydata.org/> |
| numpy              | 2.1.2         | BSD-3-Clause                  | <https://numpy.org/> |
| pyarrow            | 17.0.0        | Apache-2.0                    | <https://arrow.apache.org/> |
| apscheduler        | 3.10.4        | MIT                           | <https://github.com/agronholm/apscheduler> |
| httpx              | 0.27.2        | BSD-3-Clause                  | <https://www.python-httpx.org/> |
| requests           | 2.32.3        | Apache-2.0                    | <https://requests.readthedocs.io/> |
| pycryptodome       | 3.21.0        | BSD-2-Clause / Public Domain  | <https://www.pycryptodome.org/> |
| lxml               | 5.3.0         | BSD-3-Clause                  | <https://lxml.de/> |
| aiofiles           | 24.1.0        | Apache-2.0                    | <https://github.com/Tinche/aiofiles> |
| python-dateutil    | 2.9.0.post0   | Apache-2.0 / BSD-3-Clause     | <https://github.com/dateutil/dateutil> |
| casparser          | (latest)      | MIT                           | <https://github.com/codereverser/casparser> |

### Note on psycopg2-binary

`psycopg2-binary` is licensed under LGPL-3.0-or-later. Because Kutumb uses it
only via SQLAlchemy's standard DB-API interface (dynamic linking, no
modifications to psycopg2 itself), LGPL compatibility with the project's MIT
licence is preserved. Users redistributing Kutumb in modified form should keep
psycopg2-binary's licence notice intact and ensure they comply with LGPL
section 4.

## Runtime components (separate containers)

These run as standalone services via `docker-compose.yml` and are **not bundled
or linked** into Kutumb's Python distribution:

| Component   | License        | Notes |
|-------------|----------------|-------|
| PostgreSQL  | PostgreSQL License | Database; interacts via standard SQL |
| Grafana     | AGPL-3.0       | Dashboards; interacts via HTTP API. AGPL applies only to Grafana itself, not to Kutumb. |
| Ollama      | MIT            | Local LLM runtime |
| Llama 3.2   | Llama 3.2 Community License (Meta) | Model weights; subject to Meta's licence terms |

## Front-end / dashboards

Pre-built Grafana dashboard JSON files in `grafana/dashboards/` are authored by
the Kutumb project and licensed under the project's MIT licence.

## Reporting issues

If a licence is incorrectly attributed above, please open an issue.
