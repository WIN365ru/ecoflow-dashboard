FROM python:3.13-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY ecoflow_dashboard/ ecoflow_dashboard/

RUN pip install --upgrade pip && pip install --no-cache-dir .

VOLUME /data

ENV PYTHONUNBUFFERED=1

EXPOSE 5000

CMD ["python", "-m", "ecoflow_dashboard", "--web", "--web-port", "5000", "--db", "/data/ecoflow_history.db"]
