# ---- Stage 1: build the React SPA -----------------------------------------
FROM node:22-alpine AS frontend-build
WORKDIR /app
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

# ---- Stage 2: Python API + worker image ------------------------------------
FROM python:3.12-slim
WORKDIR /srv

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

COPY pyproject.toml README.md ./
COPY sentinel/ sentinel/
RUN pip install .

COPY alembic.ini ./
COPY alembic/ alembic/
COPY config/ config/

# Built SPA served by FastAPI at /
COPY --from=frontend-build /app/dist frontend/dist

EXPOSE 8000
CMD ["uvicorn", "sentinel.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
