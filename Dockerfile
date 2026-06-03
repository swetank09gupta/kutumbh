FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir --timeout 120 -r requirements.txt
# casparser pins a different pdfminer.six than pdfplumber; install without deps
# so pdfplumber's already-installed pdfminer.six is reused (compatible in practice).
# casparser_isin is a required sub-dependency that must be installed separately.
RUN pip install --no-cache-dir --no-deps casparser==0.7.4 \
 && pip install --no-cache-dir casparser-isin

COPY . .

RUN mkdir -p data/raw data/processed

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
