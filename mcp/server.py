"""
MCP Server para búsqueda de sentencias en BJN (Base de Jurisprudencia Nacional - Uruguay)
Fuente: https://bjn-buscador.onrender.com/

Soporta dos modos de transporte:
- stdio  (local, para desarrollo)
- HTTP   (remoto — se activa cuando existe la variable PORT)
"""

import asyncio
import os
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("BJN - Jurisprudencia Uruguay")

BASE_URL = "https://bjn-buscador.onrender.com"
POLL_INTERVAL = 3
MAX_POLLS = 30


async def _esperar_job(client: httpx.AsyncClient, job_id: str) -> dict:
    """Espera a que un job asíncrono del BJN finalice y devuelve el resultado."""
    for _ in range(MAX_POLLS):
        await asyncio.sleep(POLL_INTERVAL)
        resp = await client.get(f"{BASE_URL}/api/job/{job_id}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") not in ("pending", "running"):
            return data
    raise TimeoutError("El servidor BJN tardó demasiado en responder. Intentá de nuevo.")


@mcp.tool()
async def buscar_sentencias(
    texto: str,
    tipo_busqueda: str = "todas",
    tipo_sentencia: str = "",
    orden: str = "relevancia",
) -> str:
    """
    Busca sentencias judiciales en la Base de Jurisprudencia Nacional (BJN) de Uruguay.

    Args:
        texto: Texto o términos a buscar (ej: "responsabilidad extracontractual", "daños y perjuicios").
        tipo_busqueda: Cómo interpretar los términos. Opciones:
            - "todas"     → todas las palabras deben aparecer (por defecto)
            - "exacta"    → frase exacta
            - "alguna"    → al menos una de las palabras
            - "maximizar" → maximizar resultados
        tipo_sentencia: Filtrar por tipo. Opciones:
            - ""               → todas (por defecto)
            - "DEFINITIVA"     → solo sentencias definitivas
            - "INTERLOCUTORIA" → solo sentencias interlocutorias
        orden: Criterio de ordenamiento. Opciones:
            - "relevancia" → por relevancia (por defecto)
            - "reciente"   → más recientes primero
            - "antiguo"    → más antiguas primero

    Returns:
        Lista de sentencias encontradas con número, tipo, tribunal y extracto.
    """
    payload = {
        "texto": texto,
        "tipo": tipo_busqueda,
        "sentencia": tipo_sentencia,
        "orden": orden,
    }

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{BASE_URL}/api/buscar", json=payload)
        resp.raise_for_status()
        job_id = resp.json()["job_id"]
        resultado = await _esperar_job(client, job_id)

    resultados = resultado.get("results", [])
    total = resultado.get("total", 0)

    if not resultados:
        return f'No se encontraron sentencias para: "{texto}"'

    lineas = [f'Se encontraron {total} sentencia(s) para: "{texto}"\n']
    for r in resultados:
        lineas.append(
            f"---\n"
            f"**{r['numero']}** — {r['tipo']} | {r['tribunal']}\n"
            f"Proceso: {r['proceso']}\n"
            f"Extracto: {r['extracto']}\n"
        )

    lineas.append(
        "\nPara ver el texto completo usá la herramienta `obtener_detalle_sentencia` "
        "con el índice del resultado (0 = primera, 1 = segunda, etc.)."
    )
    return "\n".join(lineas)


@mcp.tool()
async def obtener_detalle_sentencia(indice: int = 0) -> str:
    """
    Obtiene el texto completo de una sentencia de los últimos resultados de búsqueda.

    Debe haberse realizado previamente una búsqueda con `buscar_sentencias`.
    El índice corresponde a la posición en la lista de resultados (0 = primera, 1 = segunda, etc.).

    Args:
        indice: Posición de la sentencia en los últimos resultados (0 por defecto).

    Returns:
        Texto completo de la sentencia, metadatos y enlace al BJN oficial.
    """
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{BASE_URL}/api/detalle", json={"index": indice})
        resp.raise_for_status()
        job_id = resp.json()["job_id"]
        resultado = await _esperar_job(client, job_id)

    titulo = resultado.get("titulo", "Sin título")
    detalle = resultado.get("detalle", "Sin contenido")
    url = resultado.get("popup_url", "")

    partes = [f"# {titulo}\n", detalle]
    if url:
        partes.append(f"\nFuente oficial BJN: {url}")

    return "\n".join(partes)


if __name__ == "__main__":
    port = os.environ.get("PORT")
    if port:
        mcp.run(transport="streamable-http", host="0.0.0.0", port=int(port))
    else:
        mcp.run(transport="stdio")
