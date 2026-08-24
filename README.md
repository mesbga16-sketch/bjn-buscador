# BJN Buscador

Herramienta de acceso no oficial a la [Base de Jurisprudencia Nacional pública del Poder Judicial de Uruguay][1]. Permite buscar sentencias, consultar el texto publicado, navegar los resultados y verificar citas legales contra el BJN y el registro de IMPO.

## Mejoras incorporadas

| Área | Cambio |
|---|---|
| Búsqueda | Se agregó el control de sinónimos y se conservaron los valores oficiales del BJN para relevancia, fecha descendente y fecha ascendente. |
| Enlaces compartidos | La URL conserva el texto, el modo, el orden y el uso de sinónimos; al abrirla, la búsqueda se inicia automáticamente. |
| Resultados | Se reconocen identificadores numéricos compuestos y prefijos SEF, DFA e IUE cuando aparecen en el título de una sentencia. |
| Verificador | Se amplió el reconocimiento de citas con la forma `N.º`, identificadores con guiones y se agregó cancelación de consultas largas. |
| Confiabilidad | Se incorporó `/healthz`, un estado explícito del worker de Playwright, validación de índices y direcciones, y limpieza periódica de jobs vencidos. |
| Accesibilidad | Se agregaron etiquetas asociadas a campos, estados ARIA, foco visible, navegación por teclado en las tarjetas y roles para los controles interactivos. |
| MCP | Los dos servidores MCP ahora envían los nombres de campos que espera la API, admiten sinónimos y pueden recibir `BJN_BASE_URL` por variable de entorno. |
| Pruebas | Se agregaron pruebas unitarias para extracción, orden, deduplicación y clasificación de citas. |

## Ejecución local

La aplicación requiere Python 3.11 o posterior, las dependencias de `requirements.txt` y el navegador Chromium de Playwright.

```bash
pip install -r requirements.txt
playwright install chromium
PORT=3737 python3 server.py
```

La interfaz queda disponible en `http://127.0.0.1:3737/`. El estado del proceso puede consultarse en `http://127.0.0.1:3737/healthz` y el estado específico del worker en `http://127.0.0.1:3737/api/status`.

Para ejecutar las pruebas:

```bash
python3 -m py_compile server.py citations.py mcp/server.py mcp_server.py
python3 -m unittest discover -s tests -v
```

## Despliegue en Render

El repositorio conserva el despliegue Docker existente. El contenedor instala Chromium durante la construcción y arranca Uvicorn con la aplicación ASGI combinada. El servicio publicado usa el plan gratuito de Render, por lo que puede suspenderse después de un período de inactividad y tardar en despertar ante la primera solicitud. La aplicación ahora expone el estado del worker, pero el cambio de plan de Render queda fuera del código y puede generar costos.

No se deben incluir archivos `.env` ni credenciales en el repositorio. Si el servidor MCP independiente necesita apuntar a otra instancia, se puede definir `BJN_BASE_URL` antes de iniciarlo.

## Alcance y límites

La herramienta consulta en tiempo real las fuentes públicas. No mantiene una copia propia del corpus, no reemplaza la revisión profesional de una cita y no marca como inexistente una norma cuando la fuente oficial no respondió o faltan datos para verificarla. La clasificación `no_verificable` comunica esa diferencia.

## Fuentes

[1]: https://bjn.poderjudicial.gub.uy/ "Base de Jurisprudencia Nacional pública"
[2]: https://www.impo.com.uy/bases "IMPO - Bases de datos normativas"
