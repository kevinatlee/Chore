FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=America/Vancouver

RUN groupadd --gid 1000 chore \
    && useradd --uid 1000 --gid chore --create-home chore

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir --requirement requirements.txt

COPY --chown=chore:chore . .
RUN mkdir -p /app/data /app/staticfiles \
    && chown -R chore:chore /app/data /app/staticfiles

USER chore
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD ["python", "docker/healthcheck.py"]

ENTRYPOINT ["python", "-m", "docker.entrypoint"]
