FROM python:3.11-slim

WORKDIR /app

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

COPY requirements.txt /app/

RUN pip install --no-cache-dir -r requirements.txt

COPY agent.py /app/
COPY config.py /app/
COPY metrics.py /app/
COPY observability.py /app/
COPY plugins /app/plugins

# Download turn detector model weights (cached in image layer)
RUN python agent.py download-files

CMD ["python", "agent.py", "start"]
