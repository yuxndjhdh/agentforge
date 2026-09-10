FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONUTF8=1
WORKDIR /app

COPY requirements.lock ./
RUN pip install --no-cache-dir -r requirements.lock
COPY . .
RUN pip install --no-cache-dir --no-deps -e .

EXPOSE 8000
CMD ["python", "-m", "agentforge", "serve", "--host", "0.0.0.0", "--port", "8000"]
