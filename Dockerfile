FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
# База и ключ Google монтируются в /data: docker run -v $(pwd)/data:/data --env-file .env ...
ENV DB_PATH=/data/queue_bot.db GOOGLE_CREDENTIALS=/data/credentials.json
CMD ["python", "-m", "bot"]
