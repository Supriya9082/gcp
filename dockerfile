FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY main.py .
ENV PROJECT_ID=tonal-edge-478006-u5
CMD ["python", "main.py"]


