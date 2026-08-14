FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    libgl1 \
    libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py llm.py ./
COPY services/ services/
COPY static/ static/

RUN mkdir -p data/markdown

EXPOSE 7860

CMD ["python", "app.py"]
