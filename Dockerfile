# Imagen oficial de Playwright para Python - incluye Chromium y todas las dependencias del sistema
FROM mcr.microsoft.com/playwright/python:v1.60.0-jammy

WORKDIR /app

# Copiar dependencias e instalar (playwright ya está en la imagen base, solo flask)
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copiar el resto del código
COPY . .

# Puerto expuesto
ENV PORT=10000
EXPOSE 10000

# Iniciar el servidor
CMD ["python", "server.py"]
