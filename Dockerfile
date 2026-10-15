FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 TZ=Europe/Tallinn DATA_DIR=/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends libgomp1 tzdata && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY voltee ./voltee
VOLUME /data
CMD ["python", "-m", "voltee.run", "--schedule"]
