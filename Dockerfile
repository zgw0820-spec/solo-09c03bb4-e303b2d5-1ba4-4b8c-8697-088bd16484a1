FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ ./app/
COPY scripts/ ./scripts/

EXPOSE 8000 53/udp

# Default command is overridden per service in docker-compose.yml
CMD ["python", "-m", "app.web"]
