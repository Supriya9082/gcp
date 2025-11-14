# Dockerfile (recommended)
FROM python:3.11-slim

# avoid interactive prompts and reduce layers
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PORT=8080

WORKDIR /app

# install OS deps required by some python packages (small)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# copy requirements and install
COPY requirements.txt /app/requirements.txt
RUN pip install --upgrade pip setuptools wheel \
 && pip install --no-cache-dir -r /app/requirements.txt

# copy the rest of the app
COPY . /app

# expose port and use gunicorn to serve
EXPOSE 8080
CMD ["gunicorn", "main:app", "-b", "0.0.0.0:8080", "--workers", "2", "--timeout", "120"]
