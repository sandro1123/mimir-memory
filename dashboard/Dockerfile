FROM python:3.11-slim

WORKDIR /app

# 安装依赖
COPY backend/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 复制后端
COPY backend/ /app/backend/
# 复制前端
COPY frontend/ /app/frontend/

# 环境变量
ENV MIMIR_API=http://host.docker.internal:8456
ENV CANONICAL_DB=/data/canonical.db
ENV ADMIN_TOKEN_FILE=/data/admin.token
ENV FRONTEND_DIR=/app/frontend
ENV PORT=8800

EXPOSE 8800

CMD ["python3", "-m", "uvicorn", "backend.main:app", "--host", "0.0.0.0", "--port", "8800"]