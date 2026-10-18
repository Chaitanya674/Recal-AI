# ---- frontend build ----
FROM node:20-slim AS fe
WORKDIR /fe
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ .
RUN npm run build

# ---- backend ----
FROM python:3.11-slim
WORKDIR /srv
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY backend/ backend/
COPY --from=fe /fe/dist ./frontend_dist
ENV PORT=8080
CMD exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT}
