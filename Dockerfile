FROM python:3.11-slim

# Voice channel support (libopus for discord.py[voice], ffmpeg for audio pipe)
RUN apt-get update && apt-get install -y --no-install-recommends \
        libopus0 \
        ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN chmod +x entrypoint.sh

# SQLite lives here; mount a named volume so data survives restarts
VOLUME /data

ENTRYPOINT ["./entrypoint.sh"]
