FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 TZ=Asia/Colombo
WORKDIR /app

# pdftotext reads net assets per share and market ratios from CSE report PDFs.
RUN apt-get update && apt-get install -y --no-install-recommends poppler-utils \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir .

# Run as the host user's uid (set in compose) so the SQLite file stays editable.
CMD ["uvicorn", "cse_analyzer.web:app", "--host", "0.0.0.0", "--port", "8080"]
