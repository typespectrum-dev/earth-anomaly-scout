FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    SCOUT_DB=/data/scout.db
RUN apt-get update && apt-get install -y --no-install-recommends tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY scout.py feeds.json ./
VOLUME ["/data"]
CMD ["python", "scout.py", "loop"]
