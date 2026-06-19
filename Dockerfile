# Imagen base liviana: python:3.11-slim (~150MB)
FROM python:3.11-slim

# Dependencias mínimas del sistema para Chromium headless en Linux
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

# Fijar la ruta del navegador para que Playwright no lo busque en otro lugar
# y para que Docker pueda cachear la capa si la versión no cambia
ENV PLAYWRIGHT_BROWSERS_PATH=/opt/playwright-browsers

# Instalar dependencias Python
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Instalar Chromium con todas sus dependencias del sistema en la ruta fija
# --with-deps garantiza que no falten librerías en producción
RUN playwright install --with-deps chromium

# Copiar el código de la aplicación
COPY . .

ENV PORT=10000
EXPOSE 10000

CMD ["sh", "-c", "uvicorn server:combined_app --host 0.0.0.0 --port ${PORT:-10000}"]
