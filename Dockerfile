# Imagen base liviana: python:3.11-slim (~150MB) en lugar de la imagen oficial de Playwright (~1.5GB)
FROM python:3.11-slim

# Dependencias mínimas del sistema para que Chromium headless-shell funcione en Linux
RUN apt-get update && apt-get install -y --no-install-recommends \
    libnss3 \
    libnspr4 \
    libatk1.0-0 \
    libatk-bridge2.0-0 \
    libcups2 \
    libdrm2 \
    libdbus-1-3 \
    libxkbcommon0 \
    libxcomposite1 \
    libxdamage1 \
    libxfixes3 \
    libxrandr2 \
    libgbm1 \
    libasound2 \
    libpango-1.0-0 \
    libcairo2 \
    libatspi2.0-0 \
    libwayland-client0 \
    fonts-liberation \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Instalar dependencias Python y descargar solo el binario de Chromium headless-shell
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
    && playwright install chromium

# Copiar el código de la aplicación
COPY . .

ENV PORT=10000
EXPOSE 10000

CMD ["python", "server.py"]
