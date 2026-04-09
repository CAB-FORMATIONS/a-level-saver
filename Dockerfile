FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 10000

CMD ["gunicorn", "webhook_server:app", "--bind", "0.0.0.0:10000", "--workers", "1", "--timeout", "120"]
