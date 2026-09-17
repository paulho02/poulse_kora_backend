ARG PYTHON_VER=3.13

FROM python:${PYTHON_VER} AS base

WORKDIR /app

ENV PYTHONUNBUFFERED=1

# Install Poetry
RUN curl -sSL https://install.python-poetry.org | POETRY_HOME=/opt/poetry python && \
    cd /usr/local/bin && \
    ln -s /opt/poetry/bin/poetry && \
    poetry config virtualenvs.create false

COPY ./pyproject.toml ./poetry.lock* /app/

RUN poetry install --no-root

COPY . /app

FROM python:${PYTHON_VER}

ARG PYTHON_VER

WORKDIR /app

# ffmpeg/ffprobe: used by app/core/media_validation.py to probe and re-mux uploaded
# post videos (strips container metadata, e.g. GPS, and measures duration) - not
# needed by the builder stage above, only at runtime.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY --from=base /app /app
COPY --from=base /usr/local/lib/python${PYTHON_VER}/site-packages /usr/local/lib/python${PYTHON_VER}/site-packages
COPY --from=base /usr/local/bin /usr/local/bin

RUN chmod +x /app/entrypoint.sh

# Not root. Nothing here needs it: the app listens on an unprivileged port, writes
# only to /tmp (ffmpeg scratch), and reads /app. What root would add is that a
# bug in ffmpeg or Pillow - the two things that parse untrusted bytes - lands as
# root inside the container rather than as a user that owns nothing. /app is
# chowned so `alembic revision` and the reload watcher work in the dev override,
# which mounts the source tree over it.
RUN useradd --system --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && chown -R app:app /app
USER app

CMD ["/app/entrypoint.sh"]
