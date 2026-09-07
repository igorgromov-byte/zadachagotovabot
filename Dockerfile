FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY core.py integrations.py app.py config.json ./
CMD ["python", "app.py", "run"]
