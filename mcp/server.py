"""
MCP Server para búsqueda de sentencias en BJN (Base de Jurisprudencia Nacional - Uruguay)
Fuente: https://bjn-buscador.onrender.com/
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
    for _ in range(MAX_POLLS):
        await asyncio.sleep(POLL_INTERVAL)
        resp = await client.get(f"{BASE_URL}/api/job/{job_id}")
        resp.raise_for_status()
        data = resp.json()
        if data.get("status") not in ("pending", "running"):
            return data
    raise TimeoutError("El servidor BJN tardó demasiado. Intentá de nuevo.")


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
        texto: Texto a buscar (ej: "responsabilidad extracontractual", "daños y perjuicios").
        tipo_busqueda: "todas" | "exacta" | "alguna" | "maximizar"
        tipo_sentencia: "" (todas) | "DEFINITIVA" | "INTERLOCUTORIA"
        orden: "relevancia" | "reciente" | "antiguo"

    Returns:
        Lista de sentencias con número, tipo, tribunal y extracto.
    """
    payload = {"texto": texto, "tipo": tipo_busqueda, "sentencia": tipo_sentencia, "orden": orden}

    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{BASE_URL}/api/buscar", json=payload)
        resp.raise_for_status()
        resultado = await _esperar_job(client, resp.json()["job_id"])

    resultados = resultado.get("results", [])
    if not resultados:
        return f'No se encontraron sentencias para: "{texto}"'

    lineas = [f'Se encontraron {resultado.get("total", 0)} sentencia(s) para: "{texto}"\n']
    for r in resultados:
        lineas.append(
            f"---\n**{r['numero']}** — {r['tipo']} | {r['tribunal']}\n"
            f"Proceso: {r['proceso']}\nExtracto: {r['extracto']}\n"
        )
    lineas.append("\nUsá `obtener_detalle_sentencia` con el índice (0=primera, 1=segunda…) para el texto completo.")
    return "\n".join(lineas)


@mcp.tool()
async def obtener_detalle_sentencia(indice: int = 0) -> str:
    """
    Obtiene el texto completo de una sentencia de la última búsqueda.

    Args:
        indice: Posición en los últimos resultados (0 = primera).

    Returns:
        Texto completo, metadatos y enlace al BJN oficial.
    """
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{BASE_URL}/api/detalle", json={"index": indice})
        resp.raise_for_status()
        resultado = await _esperar_job(client, resp.json()["job_id"])

    partes = [f"# {resultado.get('titulo', 'Sin título')}\n", resultado.get("detalle", "")]
    if url := resultado.get("popup_url"):
        partes.append(f"\nFuente oficial BJN: {url}")
    return "\n".join(partes)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    if os.environ.get("PORT"):
        import uvicorn
        uvicorn.run(mcp.sse_app(), host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
