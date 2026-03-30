FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/

RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py /app/
COPY config.py /app/
COPY plugins /app/plugins

CMD ["python", "agent.py", "start"]
