FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY scanner/requirements.txt ./scanner/requirements.txt
RUN pip install --no-cache-dir -r scanner/requirements.txt

COPY . .

EXPOSE 8000

CMD ["python", "main.py", "dashboard", "--port", "8000"]
