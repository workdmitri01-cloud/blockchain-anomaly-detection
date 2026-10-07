FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY alerters ./alerters
COPY config ./config
COPY data ./data
COPY scripts ./scripts
ENV PYTHONUNBUFFERED=1
# Which bot to run is chosen by the command (see docker-compose.yml).
ENTRYPOINT ["python", "-m"]
