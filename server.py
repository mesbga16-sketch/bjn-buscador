"""
BJN Buscador - Servidor Flask + Playwright
Busqueda de sentencias en la Base de Jurisprudencia Nacional (Uruguay)
v9: arquitectura simplificada con contexto persistente reutilizable.
"""

from flask import Flask, request, jsonify, send_from_directory
import re, os, time, threading, queue

app = Flask(__name__, static_folder='public')
PORT = int(os.environ.get('PORT', 3737))

BJN_SIMPLE = 'https://bjn.poderjudicial.gub.uy/BJNPUBLICA/busquedaSimple.seam'

# ─── Worker thread dedicado para Playwright ───────────────────────────────────
# Playwright sync API no puede usarse desde multiples threads.
# Solucion: un unico thread que procesa todas las operaciones del browser.

_task_queue = queue.Queue()

# Estado compartido (solo escrito/leido desde el worker thread)
_state = {
    'page': None,       # Pagina de resultados actual (reutilizada entre busquedas)
    'last_url': None,   # URL de la pagina de resultados actual
}

def _playwright_worker():
    from playwright.sync_api import sync_playwright

    pw      = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    ctx     = browser.new_context(locale='es-UY')
    page    = ctx.new_page()
    _state['page'] = page

    # ── JavaScript helpers ────────────────────────────────────────────────────

    EXTRACT_JS = """() => {
        const links = document.querySelectorAll('a[onclick*="lnkTituloSentencia"]');
        if (links.length > 0) {
            return Array.from(links).map((a, i) => {
                const tr = a.closest('tr');
                const extracto = tr ? tr.innerText.replace(a.innerText, '').trim() : '';
                return { index: i, titulo: a.innerText.trim(),
                         extracto: extracto.substring(0, 400) };
            });
        }
        return [];
    }"""

    PAGINATION_JS = """() => {
        const all = Array.from(document.querySelectorAll('a, input[type="submit"], input[type="button"]'));
        const nextEl = all.find(el => /^(siguiente|>>|>)$/i.test((el.textContent || el.value || '').trim()));
        const prevEl = all.find(el => /^(anterior|<<|<)$/i.test((el.textContent || el.value || '').trim()));
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

    # ── Funciones de busqueda ─────────────────────────────────────────────────

    def wait_results():
        try:
            page.wait_for_selector('a[onclick*="lnkTituloSentencia"]', timeout=20000)
        except Exception:
            pass

    def do_search(data):
        texto         = data.get('texto', '').strip()
        tipo_busqueda = data.get('tipoBusqueda', 'TODAS_LAS_PALABRAS')
        ordenar       = data.get('ordenar', 'RELEVANCIA')

        page.goto(BJN_SIMPLE, wait_until='domcontentloaded', timeout=25000)
        page.wait_for_selector('#formBusqueda\\:cajaQuery', timeout=10000)
        if texto:
            page.fill('#formBusqueda\\:cajaQuery', texto)
        checked = page.eval_on_selector('#formBusqueda\\:chkMasOpciones', 'el => el.checked')
        if not checked:
            page.click('#formBusqueda\\:chkMasOpciones')
            page.wait_for_timeout(500)
        page.select_option('select[name="formBusqueda:j_id44:j_id48"]', tipo_busqueda)
        page.select_option('select[name="formBusqueda:j_id52:j_id56"]', ordenar)
        page.click('#formBusqueda\\:Search')
        wait_results()

        _state['last_url'] = page.url
        raw        = page.evaluate(EXTRACT_JS)
        pagination = page.evaluate(PAGINATION_JS)
        return raw, pagination

    def do_pagina(direction):
        pats    = ['siguiente', '>>', '>'] if direction == 'next' else ['anterior', '<<', '<']
        clicked = page.evaluate(GO_PAGE_JS, pats)
        if not clicked:
            raise ValueError('No hay mas paginas.')
        page.wait_for_timeout(2500)
        wait_results()
        _state['last_url'] = page.url
        raw        = page.evaluate(EXTRACT_JS)
        pagination = page.evaluate(PAGINATION_JS)
        return raw, pagination

    def do_detalle(index):
        """
        Obtiene el texto completo de la sentencia en la posicion 'index'.
        Estrategia: interceptar window.open en la pagina principal, hacer clic,
        capturar la URL del popup, navegar a ella y luego volver a los resultados.
        """
        links = page.query_selector_all('a[onclick*="lnkTituloSentencia"]')
        if not links or index >= len(links):
            raise ValueError('Resultado no encontrado.')
        titulo = links[index].inner_text().strip()
        results_url = _state.get('last_url') or BJN_SIMPLE

        try:
            # Interceptar window.open ANTES del clic
            page.evaluate("""() => {
                window._capturedPopupUrl = null;
                window.open = function(url, name, features) {
                    window._capturedPopupUrl = url;
                    return { focus: () => {}, closed: false };
                };
            }""")

            # Hacer clic y esperar el AJAX
            links[index].click()
            page.wait_for_timeout(4000)
            popup_url = page.evaluate('() => window._capturedPopupUrl')

            if not popup_url:
                raise ValueError('No se pudo obtener la URL de la sentencia. Intentá de nuevo.')

            if popup_url.startswith('/'):
                popup_url = f'https://bjn.poderjudicial.gub.uy{popup_url}'

            # Navegar a la URL de la sentencia en la pagina principal
            page.goto(popup_url, wait_until='domcontentloaded', timeout=20000)
            detalle_text = page.evaluate("""() => {
                const box = document.getElementById('textoSentenciaBox');
                if (box) return box.innerText.trim();
                return document.body.innerText.trim();
            }""")

            return {'titulo': titulo, 'detalle': detalle_text, 'popup_url': popup_url}
        finally:
            # Volver a la pagina de resultados para que la proxima busqueda funcione
            try:
                page.goto(results_url, wait_until='domcontentloaded', timeout=20000)
                page.wait_for_selector('a[onclick*="lnkTituloSentencia"]', timeout=10000)
            except Exception:
                pass

    # ── Loop principal del worker ─────────────────────────────────────────────

    while True:
        task = _task_queue.get()
        if task is None:
            break
        holder = task.get('result')
        try:
            t = task['type']
            if t == 'status':
                holder['value'] = {'ok': True}
            elif t == 'search':
                raw, pagination = do_search(task['data'])
                holder['value'] = {'raw': raw, 'pagination': pagination}
            elif t == 'pagina':
                raw, pagination = do_pagina(task['direction'])
                holder['value'] = {'raw': raw, 'pagination': pagination}
            elif t == 'detalle':
                holder['value'] = do_detalle(task['index'])
        except Exception as e:
            holder['error'] = str(e)
        finally:
            if holder and 'event' in holder:
                holder['event'].set()


def _call_worker(task_dict, timeout=28):
    ev = threading.Event()
    holder = {'event': ev, 'value': None, 'error': None}
    task_dict['result'] = holder
    _task_queue.put(task_dict)
    ev.wait(timeout=timeout)
    if not ev.is_set():
        raise TimeoutError('El worker no respondio a tiempo.')
    if holder['error']:
        raise RuntimeError(holder['error'])
    return holder['value']


# ─── Helpers de resultados ────────────────────────────────────────────────────

def parse_title(titulo: str) -> dict:
    m = re.match(r'^(\d+/\d+)\s+(\w+)\s+-\s+(.+?)\s+-\s+(.+)$', titulo)
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

@app.route('/api/buscar', methods=['POST'])
def buscar():
    data = request.get_json() or {}
    if not data.get('texto', '').strip():
        return jsonify({'error': 'Ingrese un texto para buscar.'}), 400
    try:
        res     = _call_worker({'type': 'search', 'data': data})
        results = process_raw_results(res['raw'])
        return jsonify({'results': results, 'total': len(results),
                        'query': data.get('texto', ''),
                        'pagination': res['pagination']})
    except Exception as e:
        return jsonify({'error': f'Error al consultar el BJN: {str(e)}'}), 500

@app.route('/api/pagina', methods=['POST'])
def pagina():
    data      = request.get_json() or {}
    direction = data.get('direction', 'next')
    try:
        res     = _call_worker({'type': 'pagina', 'direction': direction})
        results = process_raw_results(res['raw'])
        return jsonify({'results': results, 'total': len(results),
                        'pagination': res['pagination']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/detalle', methods=['POST'])
def detalle():
    data  = request.get_json() or {}
    index = int(data.get('index', 0))
    try:
        res = _call_worker({'type': 'detalle', 'index': index}, timeout=28)
        return jsonify(res)
    except Exception as e:
        return jsonify({'error': f'Error al cargar la sentencia: {str(e)}'}), 500

@app.route('/api/status', methods=['GET'])
def status():
    try:
        _call_worker({'type': 'status'}, timeout=3)
        return jsonify({'ok': True})
    except Exception:
        return jsonify({'ok': False})


# ─── Arranque ─────────────────────────────────────────────────────────────────

_worker_thread = threading.Thread(
    target=_playwright_worker, daemon=True, name='playwright-worker')
_worker_thread.start()

if __name__ == '__main__':
    print(f'\nBJN Buscador en http://localhost:{PORT}\n')
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
