"""
BJN Buscador - Servidor Flask + Playwright
v12: hardening de scraping - timeouts explícitos, try/except granulares,
     BeautifulSoup para extracción de texto, PLAYWRIGHT_BROWSERS_PATH fijo.
- POST /api/buscar  -> devuelve {job_id} inmediatamente (no bloquea)
- GET  /api/job/:id -> devuelve {status:'pending'|'done'|'error', ...}
- POST /api/detalle -> idem
- POST /api/pagina  -> idem
"""

from flask import Flask, request, jsonify, send_from_directory
import re, os, uuid, threading, queue, time
from bs4 import BeautifulSoup

app = Flask(__name__, static_folder='public')
PORT = int(os.environ.get('PORT', 3737))

BJN_SIMPLE = 'https://bjn.poderjudicial.gub.uy/BJNPUBLICA/busquedaSimple.seam'

# ─── Job store ────────────────────────────────────────────────────────────────
_jobs      = {}
_jobs_lock = threading.Lock()

def _new_job():
    jid = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[jid] = {'status': 'pending', 'result': None, 'error': None}
    return jid

def _finish_job(jid, result=None, error=None):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]['status'] = 'done' if result is not None else 'error'
            _jobs[jid]['result'] = result
            _jobs[jid]['error']  = error

# ─── Worker thread dedicado para Playwright ───────────────────────────────────
_task_queue = queue.Queue()

_state = {
    'page': None,
    'ctx':  None,
}

def _playwright_worker():
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    pw      = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx     = browser.new_context(locale='es-UY')
    page    = ctx.new_page()
    _state['page'] = page
    _state['ctx']  = ctx

    # ── JavaScript helpers ────────────────────────────────────────────────────

    EXTRACT_JS = """() => {
        const links = document.querySelectorAll('a[onclick*="lnkTituloSentencia"]');
        if (!links.length) return [];
        return Array.from(links).map((a, i) => {
            const tr      = a.closest('tr');
            const extracto = tr ? tr.innerText.replace(a.innerText, '').trim() : '';
            return { index: i, titulo: a.innerText.trim(),
                     extracto: extracto.substring(0, 400) };
        });
    }"""

    PAGINATION_JS = """() => {
        const all     = Array.from(document.querySelectorAll('a, input[type="submit"], input[type="button"]'));
        const nextEl  = all.find(el => /^(siguiente|>>|>)$/i.test((el.textContent || el.value || '').trim()));
        const prevEl  = all.find(el => /^(anterior|<<|<)$/i.test((el.textContent || el.value || '').trim()));
        const pageInfo = document.querySelector('.rf-ds-pg-cnt, [class*="pageCount"], [class*="pageInfo"]');
        return { hasNext: !!nextEl, hasPrev: !!prevEl,
                 pageText: pageInfo ? pageInfo.textContent.trim().replace(/\s+/g,' ') : '' };
    }"""

    GO_PAGE_JS = """(pats) => {
        const all = Array.from(document.querySelectorAll('a, input[type="submit"], input[type="button"]'));
        for (const pat of pats) {
            const el = all.find(e => (e.textContent || e.value || '').trim().toLowerCase() === pat);
            if (el) { el.click(); return true; }
        }
        return false;
    }"""

    # ── Funciones de búsqueda ─────────────────────────────────────────────────

    def wait_results(timeout_ms=25000):
        """Espera a que aparezcan links de resultados. No lanza excepción si no hay."""
        try:
            page.wait_for_selector('a[onclick*="lnkTituloSentencia"]', timeout=timeout_ms)
        except Exception:
            pass

    def do_search(data):
        texto         = data.get('texto', '').strip()
        tipo_busqueda = data.get('tipoBusqueda', 'TODAS_LAS_PALABRAS')
        ordenar       = data.get('ordenar', 'RELEVANCIA')

        # Navegar al buscador con timeout generoso para cold start
        page.goto(BJN_SIMPLE, wait_until='domcontentloaded', timeout=40000)
        page.wait_for_selector('#formBusqueda\\:cajaQuery', timeout=15000)

        if texto:
            page.fill('#formBusqueda\\:cajaQuery', texto)

        # Mostrar opciones avanzadas si están ocultas
        try:
            checked = page.eval_on_selector('#formBusqueda\\:chkMasOpciones', 'el => el.checked')
            if not checked:
                page.click('#formBusqueda\\:chkMasOpciones')
                page.wait_for_timeout(500)
        except Exception:
            pass  # El checkbox puede no existir en todos los estados

        try:
            page.select_option('select[name="formBusqueda:j_id44:j_id48"]', tipo_busqueda)
        except Exception:
            pass

        try:
            page.select_option('select[name="formBusqueda:j_id52:j_id56"]', ordenar)
        except Exception:
            pass

        page.click('#formBusqueda\\:Search')
        wait_results()

        raw        = page.evaluate(EXTRACT_JS)
        pagination = page.evaluate(PAGINATION_JS)
        return raw, pagination

    def do_pagina(direction):
        pats    = ['siguiente', '>>', '>'] if direction == 'next' else ['anterior', '<<', '<']
        clicked = page.evaluate(GO_PAGE_JS, pats)
        if not clicked:
            raise ValueError('No hay más páginas.')
        page.wait_for_timeout(2500)
        wait_results()
        raw        = page.evaluate(EXTRACT_JS)
        pagination = page.evaluate(PAGINATION_JS)
        return raw, pagination

    def do_detalle(index):
        """
        Obtiene el texto completo de la sentencia en la posición 'index'.

        Estrategia robusta:
        1. ctx.expect_page() captura el popup real que el BJN abre via window.open
           (más confiable que interceptar window.open manualmente).
        2. popup_page.content() + BeautifulSoup extrae el texto del HTML directamente,
           sin depender del rendering CSS (funciona en headless sin GPU).
        3. Timeouts explícitos en cada paso con fallback graceful.
        """
        links = page.query_selector_all('a[onclick*="lnkTituloSentencia"]')
        if not links or index >= len(links):
            raise ValueError('Resultado no encontrado.')

        titulo = links[index].inner_text().strip()

        # ── Paso 1: capturar el popup ─────────────────────────────────────────
        popup_page = None
        try:
            with ctx.expect_page(timeout=22000) as popup_info:
                links[index].click()
            popup_page = popup_info.value
        except PWTimeout:
            raise ValueError('Esta sentencia no tiene texto publicado en el BJN.')
        except Exception as e:
            raise ValueError(f'No se pudo abrir el detalle: {e}')

        # ── Paso 2: esperar carga del popup ───────────────────────────────────
        try:
            popup_page.wait_for_load_state('domcontentloaded', timeout=15000)
        except PWTimeout:
            pass  # Continuar con lo que haya cargado
        except Exception:
            pass

        try:
            # networkidle asegura que el AJAX de RichFaces terminó
            popup_page.wait_for_load_state('networkidle', timeout=8000)
        except Exception:
            pass  # No es crítico; continuar igual

        # ── Paso 3: extraer HTML y cerrar popup ───────────────────────────────
        popup_url = popup_page.url
        try:
            html = popup_page.content()
        except Exception as e:
            try:
                popup_page.close()
            except Exception:
                pass
            raise ValueError(f'No se pudo leer el contenido del popup: {e}')
        finally:
            try:
                popup_page.close()
            except Exception:
                pass

        # ── Paso 4: parsear con BeautifulSoup ────────────────────────────────
        # BeautifulSoup extrae el texto del HTML sin depender del rendering CSS.
        # Esto es necesario en entornos headless sin GPU donde innerText puede
        # devolver '' aunque el HTML tenga contenido.
        soup = BeautifulSoup(html, 'html.parser')
        for tag in soup(['script', 'style', 'head']):
            tag.decompose()
        detalle_text = soup.get_text(separator='\n', strip=True)

        return {'titulo': titulo, 'detalle': detalle_text, 'popup_url': popup_url}

    # ── Loop principal del worker ─────────────────────────────────────────────

    while True:
        task = _task_queue.get()
        if task is None:
            break
        jid = task.get('jid')
        try:
            t = task['type']
            if t == 'status':
                _finish_job(jid, result={'ok': True})
            elif t == 'search':
                raw, pagination = do_search(task['data'])
                results = process_raw_results(raw)
                _finish_job(jid, result={
                    'results': results, 'total': len(results),
                    'query': task['data'].get('texto', ''),
                    'pagination': pagination
                })
            elif t == 'pagina':
                raw, pagination = do_pagina(task['direction'])
                results = process_raw_results(raw)
                _finish_job(jid, result={
                    'results': results, 'total': len(results),
                    'pagination': pagination
                })
            elif t == 'detalle':
                res = do_detalle(task['index'])
                _finish_job(jid, result=res)
        except Exception as e:
            _finish_job(jid, error=str(e))


# ─── Helpers de resultados ────────────────────────────────────────────────────

def parse_title(titulo: str) -> dict:
    m = re.match(r'^(\d[\d.]*\/\d+)\s+(\w+)\s+-\s+(.+?)\s+-\s+(.+)$', titulo)
    if m:
        return {'numero': m.group(1), 'tipo': m.group(2),
                'tribunal': m.group(3).strip(), 'proceso': m.group(4).strip()}
    return {'numero': '', 'tipo': '', 'tribunal': '', 'proceso': titulo}

def process_raw_results(raw: list) -> list:
    return [{**r, **parse_title(r['titulo'])} for r in raw]


# ─── Rutas ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/api/job/<jid>', methods=['GET'])
def get_job(jid):
    with _jobs_lock:
        job = _jobs.get(jid)
    if not job:
        return jsonify({'status': 'not_found'}), 404
    if job['status'] == 'pending':
        return jsonify({'status': 'pending'})
    if job['status'] == 'error':
        with _jobs_lock:
            _jobs.pop(jid, None)
        return jsonify({'status': 'error', 'error': job['error']}), 500
    result = job['result']
    with _jobs_lock:
        _jobs.pop(jid, None)
    return jsonify({'status': 'done', **result})

def _submit_job(task_dict):
    jid = _new_job()
    task_dict['jid'] = jid
    _task_queue.put(task_dict)
    return jid

@app.route('/api/buscar', methods=['POST'])
def buscar():
    data = request.get_json() or {}
    if not data.get('texto', '').strip():
        return jsonify({'error': 'Ingrese un texto para buscar.'}), 400
    jid = _submit_job({'type': 'search', 'data': data})
    return jsonify({'job_id': jid})

@app.route('/api/pagina', methods=['POST'])
def pagina():
    data      = request.get_json() or {}
    direction = data.get('direction', 'next')
    jid = _submit_job({'type': 'pagina', 'direction': direction})
    return jsonify({'job_id': jid})

@app.route('/api/detalle', methods=['POST'])
def detalle():
    data  = request.get_json() or {}
    index = int(data.get('index', 0))
    jid = _submit_job({'type': 'detalle', 'index': index})
    return jsonify({'job_id': jid})

@app.route('/api/status', methods=['GET'])
def status():
    jid = _new_job()
    _task_queue.put({'type': 'status', 'jid': jid})
    for _ in range(15):
        time.sleep(0.2)
        with _jobs_lock:
            job = _jobs.get(jid, {})
        if job.get('status') != 'pending':
            with _jobs_lock:
                _jobs.pop(jid, None)
            return jsonify({'ok': job.get('status') == 'done'})
    with _jobs_lock:
        _jobs.pop(jid, None)
    return jsonify({'ok': False})


# ─── MCP Server (Streamable HTTP) ─────────────────────────────────────────────
# Expone las herramientas de búsqueda en /mcp para usar desde Claude Code.
# Configuración en ~/.claude/settings.json:
#   { "mcpServers": { "bjn": { "type": "http", "url": "https://bjn-buscador.onrender.com/mcp" } } }

import httpx as _httpx
from mcp.server.fastmcp import FastMCP as _FastMCP

_mcp = _FastMCP(
    'BJN Jurisprudencia',
    instructions=(
        'Buscador de sentencias del Poder Judicial de Uruguay (BJN). '
        'Usa buscar_jurisprudencia para encontrar sentencias. '
        'Usa obtener_detalle con el index del resultado para leer el texto completo. '
        'Usa navegar_pagina con next o prev para paginar resultados.'
    ),
)

_LOCAL = f'http://127.0.0.1:{PORT}'

_TIPO_MAP = {'todas': 'TODAS_LAS_PALABRAS', 'frase': 'FRASE_EXACTA', 'alguna': 'ALGUNA_PALABRA'}
_ORDEN_MAP = {'relevancia': 'RELEVANCIA', 'fecha': 'FECHA'}


def _poll_local(job_id: str, timeout: int = 90) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = _httpx.get(f'{_LOCAL}/api/job/{job_id}', timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get('status') in ('done', 'error'):
            return data
        time.sleep(3)
    raise TimeoutError(f'Job {job_id} no termino en {timeout}s')


def _fmt(results: list) -> str:
    lines = []
    for r in results:
        num = r.get('numero') or r.get('titulo', '?')
        lines.append(
            f"[index={r.get('index','?')}] **{num}** — {r.get('tipo','')} | {r.get('tribunal','')}\n"
            f"{r.get('proceso','')}\n{r.get('extracto','')[:400]}\n"
        )
    return '\n'.join(lines)


@_mcp.tool()
def buscar_jurisprudencia(texto: str, modo: str = 'todas', orden: str = 'relevancia') -> str:
    """
    Busca sentencias en la Base de Jurisprudencia Nacional (BJN) de Uruguay.

    Args:
        texto: Palabras clave o frase a buscar.
        modo: 'todas' (todas las palabras), 'frase' (frase exacta) o 'alguna' (alguna palabra).
        orden: 'relevancia' o 'fecha'.

    Returns:
        Lista de sentencias con su index, número, tribunal y extracto.
    """
    payload = {
        'texto': texto,
        'tipoBusqueda': _TIPO_MAP.get(modo, 'TODAS_LAS_PALABRAS'),
        'ordenar': _ORDEN_MAP.get(orden, 'RELEVANCIA'),
    }
    r = _httpx.post(f'{_LOCAL}/api/buscar', json=payload, timeout=30)
    r.raise_for_status()
    data = _poll_local(r.json()['job_id'])
    if data.get('status') == 'error':
        return f"Error: {data.get('error', 'desconocido')}"
    results = data.get('results', [])
    if not results:
        return f"No se encontraron sentencias para: {texto!r}"
    extra = '\n(Hay más resultados — usa navegar_pagina("next"))' if data.get('pagination', {}).get('hasNext') else ''
    return f"Se encontraron {data.get('total', len(results))} sentencias para '{texto}':\n\n" + _fmt(results) + extra


@_mcp.tool()
def obtener_detalle(index: int) -> str:
    """
    Obtiene el texto completo de una sentencia. Usar después de buscar_jurisprudencia.

    Args:
        index: El número de index devuelto por buscar_jurisprudencia.

    Returns:
        Texto completo de la sentencia.
    """
    r = _httpx.post(f'{_LOCAL}/api/detalle', json={'index': index}, timeout=30)
    r.raise_for_status()
    data = _poll_local(r.json()['job_id'], timeout=120)
    if data.get('status') == 'error':
        return f"Error: {data.get('error', 'desconocido')}"
    detalle = data.get('detalle', '')
    if not detalle:
        return f'No hay texto publicado para la sentencia con index={index}.'
    return f"**{data.get('titulo', '')}**\n\n{detalle}"


@_mcp.tool()
def navegar_pagina(direccion: str = 'next') -> str:
    """
    Navega entre páginas de resultados. Usar después de buscar_jurisprudencia.

    Args:
        direccion: 'next' para la página siguiente, 'prev' para la anterior.

    Returns:
        Lista de sentencias de la nueva página.
    """
    if direccion not in ('next', 'prev'):
        return "La dirección debe ser 'next' o 'prev'."
    r = _httpx.post(f'{_LOCAL}/api/pagina', json={'direction': direccion}, timeout=30)
    r.raise_for_status()
    data = _poll_local(r.json()['job_id'])
    if data.get('status') == 'error':
        return f"Error: {data.get('error', 'desconocido')}"
    results = data.get('results', [])
    if not results:
        return 'No hay más resultados en esa dirección.'
    return _fmt(results)


# App ASGI combinada: /mcp → MCP, todo lo demás → Flask
from a2wsgi import WSGIMiddleware as _WSGIMiddleware

_flask_asgi = _WSGIMiddleware(app)
_mcp_asgi   = _mcp.streamable_http_app()


async def combined_app(scope, receive, send):
    path = scope.get('path', '')
    if scope.get('type') in ('http', 'websocket') and path.startswith('/mcp'):
        await _mcp_asgi(scope, receive, send)
    else:
        await _flask_asgi(scope, receive, send)


# ─── Arranque ─────────────────────────────────────────────────────────────────

_worker_thread = threading.Thread(
    target=_playwright_worker, daemon=True, name='playwright-worker')
_worker_thread.start()

if __name__ == '__main__':
    import uvicorn
    print(f'\nBJN Buscador + MCP en http://localhost:{PORT}\n')
    uvicorn.run(combined_app, host='0.0.0.0', port=PORT)
