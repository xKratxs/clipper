FROM python:3.12-slim

# ffmpeg para cortar/renderizar, fuente en negrita para los subtitulos
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY static ./static

# /data con permisos abiertos: Hugging Face Spaces ejecuta el contenedor con un usuario sin privilegios
RUN mkdir -p /data && chmod 777 /data

# /data debe ser un volumen persistente: ahi quedan los clips, la cola y los modelos de whisper
ENV DATA_DIR=/data \
    HF_HOME=/data/hf \
    SUB_FONT="DejaVu Sans" \
    PYTHONUNBUFFERED=1

EXPOSE 8000
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
