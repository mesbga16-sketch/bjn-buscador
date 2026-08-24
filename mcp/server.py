"""
MCP Server para búsqueda de sentencias en BJN (Base de Jurisprudencia Nacional - Uruguay)
Fuente: https://bjn-buscador.onrender.com/
"""

import asyncio
import json
import os
import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("BJN - Jurisprudencia Uruguay")

BASE_URL = os.environ.get("BJN_BASE_URL", "https://bjn-buscador.onrender.com")
POLL_INTERVAL = 3
MAX_POLLS = 30

SERVER_CARD = {
    "serverInfo": {"name": "BJN - Jurisprudencia Uruguay", "version": "1.0.0"},
    "authentication": {"required": False},
    "tools": [
        {
            "name": "buscar_sentencias",
            "description": "Busca sentencias judiciales en la Base de Jurisprudencia Nacional (BJN) de Uruguay por texto libre, con filtros de tipo y orden.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string", "description": "Texto a buscar"},
                    "tipo_busqueda": {"type": "string", "enum": ["todas", "exacta", "alguna", "maximizar"], "default": "todas"},
                    "tipo_sentencia": {"type": "string", "enum": ["", "DEFINITIVA", "INTERLOCUTORIA"], "default": ""},
                    "orden": {"type": "string", "enum": ["relevancia", "reciente", "antiguo"], "default": "relevancia"},
                    "sinonimos": {"type": "boolean", "description": "Habilita los sinónimos del BJN", "default": False},
                },
                "required": ["texto"],
            },
        },
        {
            "name": "obtener_detalle_sentencia",
            "description": "Obtiene el texto completo de una sentencia de los últimos resultados de búsqueda.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "indice": {"type": "integer", "description": "Posición en los resultados (0 = primera)", "default": 0}
                },
            },
        },
        {
            "name": "verificar_texto",
            "description": "Extrae y verifica citas legales uruguayas (leyes, decretos, jurisprudencia) contra IMPO y el BJN para detectar alucinaciones de IA.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "texto": {"type": "string", "description": "Texto a analizar en busca de citas legales"}
                },
                "required": ["texto"],
            },
        },
    ],
    "resources": [],
    "prompts": [],
}


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
    sinonimos: bool = False,
) -> str:
    """
    Busca sentencias judiciales en la Base de Jurisprudencia Nacional (BJN) de Uruguay.

    Args:
        texto: Texto a buscar (ej: "responsabilidad extracontractual", "daños y perjuicios").
        tipo_busqueda: "todas" | "exacta" | "alguna" | "maximizar"
        tipo_sentencia: "" (todas) | "DEFINITIVA" | "INTERLOCUTORIA"
        orden: "relevancia" | "reciente" | "antiguo"
        sinonimos: habilita los sinónimos del BJN cuando es true.

    Returns:
        Lista de sentencias con número, tipo, tribunal y extracto.
    """
    tipo_map = {
        "todas": "TODAS_LAS_PALABRAS",
        "exacta": "FRASE_EXACTA",
        "alguna": "ALGUNA_PALABRA",
        "maximizar": "MAXIMIZAR_RESULTADOS",
    }
    orden_map = {
        "relevancia": "RELEVANCIA",
        "reciente": "FECHA_DESCENDENTE",
        "antiguo": "FECHA_ASCENDENTE",
    }
    payload = {
        "texto": texto,
        "tipoBusqueda": tipo_map.get(tipo_busqueda, "TODAS_LAS_PALABRAS"),
        "tipoSentencia": tipo_sentencia,
        "ordenar": orden_map.get(orden, "RELEVANCIA"),
        "sinonimos": bool(sinonimos),
    }

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


_ESTADO_ICONO = {"verificada": "✅", "no_encontrada": "❌", "no_verificable": "⚠️"}


def _fmt_citas(citas: list) -> str:
    lineas = []
    for c in citas:
        icono = _ESTADO_ICONO.get(c["estado"], "?")
        lineas.append(
            f"{icono} **{c['cita']}** ({c['fuente']}) — {c['estado']}\n{c.get('detalle', '')}"
            + (f"\n{c['url']}" if c.get("url") else "")
        )
    return "\n\n".join(lineas)


@mcp.tool()
async def verificar_texto(texto: str) -> str:
    """
    Extrae citas legales uruguayas (leyes, decretos, jurisprudencia) de un texto y
    verifica contra fuentes oficiales (IMPO, BJN) si existen realmente. Sirve para
    detectar citas alucinadas por IA en escritos, dictámenes o resoluciones.

    Args:
        texto: Texto a analizar en busca de citas legales.

    Returns:
        Por cada cita: si fue verificada, no encontrada, o no se pudo verificar, con enlace.
    """
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.post(f"{BASE_URL}/api/verificar", json={"texto": texto})
        resp.raise_for_status()
        resultado = await _esperar_job(client, resp.json()["job_id"])

    citas = resultado.get("citas", [])
    if not citas:
        return "No se encontraron citas legales reconocibles en el texto."
    return f"Se encontraron {len(citas)} cita(s):\n\n" + _fmt_citas(citas)


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8000))
    if os.environ.get("PORT"):
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse, Response
        from starlette.routing import Mount, Route
        import uvicorn

        async def server_card(request: Request) -> Response:
            return JSONResponse(SERVER_CARD)

        mcp_app = mcp.streamable_http_app()

        app = Starlette(routes=[
            Route("/.well-known/mcp/server-card.json", server_card),
            Mount("/", app=mcp_app),
        ])

        uvicorn.run(app, host="0.0.0.0", port=port)
    else:
        mcp.run(transport="stdio")
