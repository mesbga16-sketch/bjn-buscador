"""
MCP Server para BJN Buscador
Permite usar el buscador de jurisprudencia uruguaya desde Claude Code.

Instalacion:
    pip install mcp[cli] httpx

Agregar a ~/.claude/settings.json:
    {
      "mcpServers": {
        "bjn": {
          "command": "python",
          "args": ["/ruta/a/mcp_server.py"]
        }
      }
    }
"""

import time
import httpx
from mcp.server.fastmcp import FastMCP

BASE_URL = "https://bjn-buscador.onrender.com"

mcp = FastMCP(
    "BJN Jurisprudencia",
    instructions=(
        "Buscador de sentencias del Poder Judicial de Uruguay (BJN). "
        "Usa buscar_jurisprudencia para encontrar sentencias. "
        "Usa obtener_detalle con el 'index' del resultado para leer el texto completo. "
        "Usa navegar_pagina con 'next' o 'prev' para paginar los resultados."
    ),
)

TIPO_BUSQUEDA = {
    "todas": "TODAS_LAS_PALABRAS",
    "frase": "FRASE_EXACTA",
    "alguna": "ALGUNA_PALABRA",
}

ORDENAR = {
    "relevancia": "RELEVANCIA",
    "fecha": "FECHA",
}


def _poll_job(job_id: str, timeout: int = 90) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = httpx.get(f"{BASE_URL}/api/job/{job_id}", timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("status") in ("done", "error"):
            return data
        time.sleep(3)
    raise TimeoutError(f"El job {job_id} no termino en {timeout}s")


def _fmt_results(results: list) -> str:
    lineas = []
    for r in results:
        idx = r.get("index", "?")
        num = r.get("numero") or r.get("titulo", "?")
        tribunal = r.get("tribunal", "")
        tipo = r.get("tipo", "")
        proceso = r.get("proceso", "")
        extracto = r.get("extracto", "")[:400]
        lineas.append(
            f"[index={idx}] **{num}** - {tipo} | {tribunal}\n"
            f"{proceso}\n{extracto}\n"
        )
    return "\n".join(lineas)


@mcp.tool()
def buscar_jurisprudencia(
    texto: str,
    modo: str = "todas",
    orden: str = "relevancia",
) -> str:
    """
    Busca sentencias en la Base de Jurisprudencia Nacional (BJN) de Uruguay.

    Args:
        texto: Palabras clave o frase a buscar en las sentencias.
        modo: Modo de busqueda - "todas" (todas las palabras), "frase" (frase exacta), "alguna" (alguna palabra).
        orden: Orden de resultados - "relevancia" o "fecha".

    Returns:
        Lista de sentencias encontradas. Cada resultado incluye un 'index' para usar con obtener_detalle.
    """
    payload = {
        "texto": texto,
        "tipoBusqueda": TIPO_BUSQUEDA.get(modo, "TODAS_LAS_PALABRAS"),
        "ordenar": ORDENAR.get(orden, "RELEVANCIA"),
    }
    r = httpx.post(f"{BASE_URL}/api/buscar", json=payload, timeout=30)
    r.raise_for_status()
    job_id = r.json()["job_id"]

    result = _poll_job(job_id)

    if result.get("status") == "error":
        return f"Error en la busqueda: {result.get('error', 'desconocido')}"

    results = result.get("results", [])
    total = result.get("total", len(results))
    pagination = result.get("pagination", {})

    if not results:
        return f"No se encontraron sentencias para: {texto!r}"

    header = f"Se encontraron {total} sentencias para '{texto}':\n"
    pag_info = ""
    if pagination.get("hasNext"):
        pag_info = "\n(Hay mas resultados - usa navegar_pagina('next') para ver la siguiente pagina)"

    return header + "\n" + _fmt_results(results) + pag_info


@mcp.tool()
def obtener_detalle(index: int) -> str:
    """
    Obtiene el texto completo de una sentencia del BJN.
    Debe llamarse despues de buscar_jurisprudencia, usando el 'index' del resultado.

    Args:
        index: Numero de indice del resultado (campo 'index' devuelto por buscar_jurisprudencia).

    Returns:
        Texto completo de la sentencia.
    """
    r = httpx.post(f"{BASE_URL}/api/detalle", json={"index": index}, timeout=30)
    r.raise_for_status()
    job_id = r.json()["job_id"]

    result = _poll_job(job_id, timeout=120)

    if result.get("status") == "error":
        return f"Error al obtener la sentencia: {result.get('error', 'desconocido')}"

    titulo = result.get("titulo", "")
    detalle = result.get("detalle", "")

    if not detalle:
        return f"No se pudo obtener el texto de la sentencia (index={index})."

    return f"**{titulo}**\n\n{detalle}"


@mcp.tool()
def navegar_pagina(direccion: str = "next") -> str:
    """
    Navega a la pagina siguiente o anterior de los resultados activos.
    Solo funciona despues de haber llamado a buscar_jurisprudencia.

    Args:
        direccion: "next" para la siguiente pagina, "prev" para la anterior.

    Returns:
        Lista de sentencias de la nueva pagina.
    """
    if direccion not in ("next", "prev"):
        return "La direccion debe ser 'next' o 'prev'."

    r = httpx.post(f"{BASE_URL}/api/pagina", json={"direction": direccion}, timeout=30)
    r.raise_for_status()
    job_id = r.json()["job_id"]

    result = _poll_job(job_id)

    if result.get("status") == "error":
        return f"Error al navegar: {result.get('error', 'desconocido')}"

    results = result.get("results", [])
    pagination = result.get("pagination", {})

    if not results:
        return "No hay mas resultados en esta direccion."

    pag_info = ""
    if direccion == "next" and not pagination.get("hasNext"):
        pag_info = "\n(Esta es la ultima pagina)"
    elif direccion == "prev" and not pagination.get("hasPrev"):
        pag_info = "\n(Esta es la primera pagina)"

    return _fmt_results(results) + pag_info


if __name__ == "__main__":
    mcp.run()
