# Microsoft Foundry Hosted Agents require linux/amd64 images.
# Build from Apple Silicon / ARM with:
#   docker build --platform linux/amd64 -t <registry>/enrichment-validation-agent:latest .
FROM --platform=linux/amd64 python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8088

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8088
CMD ["python", "foundry_app.py"]
