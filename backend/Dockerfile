# The plan reader's API, as a container.
#
# Built in two stages so the image carries the installed packages and not the
# toolchain that built them — the difference is several hundred megabytes on an
# image this size.
#
# This build includes character recognition (PaddleOCR), which is what lets a
# scanned, image-only sheet be read. It is also the single largest thing in the
# image, so a deployment that only ever sees drawings with a text layer can
# leave it out: see BUILD_WITH_OCR below.

# --- build ----------------------------------------------------------------
FROM python:3.11-slim AS build

# PaddlePaddle and OpenCV need a compiler and a few native libraries present
# while their wheels are installed.
RUN apt-get update && apt-get install --no-install-recommends -y \
        build-essential \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /install
# The build context is the project root, not backend/, because the image also
# needs /config alongside the code.
COPY backend/requirements.txt .

# Set to "false" for a much smaller image without scanned-sheet support. The
# reader handles its absence: a page with no text layer is reported as not
# read by character recognition, never as empty.
ARG BUILD_WITH_OCR=true
RUN python -m venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH"
RUN if [ "$BUILD_WITH_OCR" = "true" ]; then \
        pip install --no-cache-dir -r requirements.txt ; \
    else \
        grep -viE "^(paddlepaddle|paddleocr)" requirements.txt > slim.txt && \
        pip install --no-cache-dir -r slim.txt ; \
    fi

# --- run ------------------------------------------------------------------
FROM python:3.11-slim

# Only the shared libraries the installed packages load at runtime.
RUN apt-get update && apt-get install --no-install-recommends -y \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /opt/venv /opt/venv
ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # PaddlePaddle's default CPU acceleration crashes on the recognition
    # models on some machines. Disabling it costs speed and nothing else.
    PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=False

WORKDIR /app

# The application code and the project files it reads at runtime. The
# configuration is copied in rather than baked into the code, so a deployment
# can be retuned for a different office by replacing one file.
COPY backend/ /app/backend/
COPY config/ /app/config/
COPY tests/ /app/tests/

# The folders the reader writes into. A run's outputs live here for as long as
# the reader is looking at them; each new upload clears what came before it, so
# the container needs no persistent disk.
RUN mkdir -p /app/output/plan /app/logs /app/input /app/data

# Never run as root.
RUN useradd --create-home --uid 10001 loopsite \
    && chown -R loopsite:loopsite /app
USER loopsite

WORKDIR /app/backend

# The host tells the container which port to listen on.
ENV PORT=8000
EXPOSE 8000

# One worker on purpose: a run's files are written to the container's own disk
# and read back by the same process. Several workers would each answer for
# uploads the others hold. Scale by running more containers, not more workers.
CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1 --timeout-keep-alive 120"]
