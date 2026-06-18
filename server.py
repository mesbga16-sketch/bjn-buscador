"""
BJN Buscador - Servidor Flask + Playwright
Busqueda de sentencias en la Base de Jurisprudencia Nacional (Uruguay)
v7: worker thread dedicado para Playwright (resuelve el error "cannot switch to a different thread").
    Todas las operaciones del browser se ejecutan en un unico thread.
"""

from flask import Flask, request, jsonify, send_from_directory, Response
import re, os, time, uuid, threading, queue

app = Flask(__name__, static_folder='public')
PORT = int(os.environ.get('PORT', 3737))

BJN_SIMPLE = 'https://bjn.poderjudicial.gub.uy/BJNPUBLICA/busquedaSimple.seam'

# ─── Worker thread dedicado para Playwright ───────────────────────────────────
# Playwright sync API no puede usarse desde multiples threads.
# Solución: un unico thread que procesa todas las operaciones del browser.

_task_queue = queue.Queue()

def _playwright_worker():
    """
    Worker thread que ejecuta todas las operaciones de Playwright.
    Corre en un unico thread para evitar el error "cannot switch to a different thread".
    """
    from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout

    pw      = sync_playwright().start()
    browser = pw.chromium.launch(headless=True)
    sel_page = None  # Pagina selectiva precalentada

    def new_ctx_page():
        ctx  = browser.new_context(locale='es-UY')
        page = ctx.new_page()
        return page

    def load_selectiva():
        nonlocal sel_page
        try:
            page = new_ctx_page()
            page.goto(BJN_SIMPLE, wait_until='domcontentloaded', timeout=30000)
            page.wait_for_selector('a:has-text("Selectiva")', timeout=10000)
            page.locator('a:has-text("Selectiva")').click()
            deadline = time.time() + 20
            while time.time() < deadline:
                if 'busquedaSelectiva' in page.url:
                    break
                page.wait_for_timeout(300)
            if 'busquedaSelectiva' not in page.url:
                page.context.close()
                return False
            page.wait_for_selector(
                'input[name*="fechaDesdeCalInputDate"], input[name*="ayudanteNumero"]',
                timeout=60000
            )
            page.wait_for_timeout(1000)
            if sel_page is not None:
                try: sel_page.context.close()
                except: pass
            sel_page = page
            return True
        except Exception as e:
            print(f'[WARN] Error cargando pagina selectiva: {e}')
            return False

    def get_sel_page():
        nonlocal sel_page
        if sel_page is not None:
            try:
                if 'busquedaSelectiva' in sel_page.url:
                    return sel_page
            except Exception:
                pass
        load_selectiva()
        return sel_page

    def limpiar_formulario(page):
        try:
            btn = page.locator(
                'input[value*="impiar"], a:has-text("Limpiar"), '
                'input[name*="Clear"], input[name*="clear"]'
            )
            if btn.count() == 0:
                return False
            btn.first.click()
            page.wait_for_timeout(2000)
            page.wait_for_selector('input[name*="ayudanteResumen"]', timeout=8000)
            return True
        except Exception:
            return False

    def fill_verified(page, selector, value, max_attempts=3):
        for attempt in range(max_attempts):
            el = page.locator(selector)
            if el.count() == 0:
                return False
            el.first.fill(value)
            page.wait_for_timeout(300)
            if el.first.input_value() == value:
                return True
            page.wait_for_timeout(500 * (attempt + 1))
        return False

    def fill_simple(page, texto, tipo_busqueda, ordenar):
        page.goto(BJN_SIMPLE, wait_until='domcontentloaded', timeout=20000)
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

    def fill_selectiva(page, texto, fecha_desde, fecha_hasta, numero, procedimiento, resumen, sede):
        if texto:
            filled = False
            for sel in ['textarea[name*="cajaQuery"]', 'textarea[name*="query"]', 'textarea[name*="Query"]']:
                el = page.locator(sel)
                if el.count() > 0:
                    el.first.fill(texto)
                    page.wait_for_timeout(300)
                    if el.first.input_value() == texto:
                        filled = True
                        break
            if not filled:
                textareas = page.locator('textarea')
                for i in range(textareas.count()):
                    t = textareas.nth(i)
                    if t.is_visible():
                        t.fill(texto)
                        page.wait_for_timeout(300)
                        if t.input_value() == texto:
                            break
        if fecha_desde:
            fill_verified(page, 'input[name*="fechaDesdeCalInputDate"]', fecha_desde)
            page.locator('input[name*="fechaDesdeCalInputDate"]').first.press('Tab')
            page.wait_for_timeout(1500)
        if fecha_hasta:
            fill_verified(page, 'input[name*="fechaHastaCalInputDate"]', fecha_hasta)
            page.locator('input[name*="fechaHastaCalInputDate"]').first.press('Tab')
            page.wait_for_timeout(1500)
        if numero:
            fill_verified(page, 'input[name*="ayudanteNumero"]', numero)
        if procedimiento:
            fill_verified(page, 'input[name*="ayudanteProc"]', procedimiento)
        if resumen:
            fill_verified(page, 'input[name*="ayudanteResumen"]', resumen)
        if sede:
            for sel in ['input[name*="ayudanteSede"]', 'input[name*="Sede"]', 'input[name*="sede"]']:
                try:
                    el = page.locator(sel)
                    if el.count() > 0:
                        fill_verified(page, sel, sede)
                        break
                except Exception:
                    pass
        page.locator('input[name="formBusqueda:j_id20:Search"]').click()

    EXTRACT_JS = (
        "() => {"
        "  const links = document.querySelectorAll('a[onclick*=\"lnkTituloSentencia\"]');"
        "  if (links.length > 0) {"
        "    const out = [];"
        "    links.forEach((a, i) => {"
        "      const tr = a.closest('tr');"
        "      const extracto = tr ? tr.innerText.replace(a.innerText, '').trim() : '';"
        "      out.push({ index: i, titulo: a.innerText.trim(),"
        "                 extracto: extracto.substring(0, 400), modo: 'simple' });"
        "    });"
        "    return out;"
        "  }"
        "  const celdas = document.querySelectorAll('td[id*=\"dataTable:\"][id*=\":colFec\"]');"
        "  if (celdas.length > 0) {"
        "    const out = [];"
        "    celdas.forEach((td, i) => {"
        "      const tds = Array.from(td.closest('tr').querySelectorAll('td'));"
        "      const cols = tds.map(t => t.innerText.trim());"
        "      const titulo = [cols[2], cols[1], cols[3]].filter(Boolean).join(' - ');"
        "      const extracto = (cols[0] || '') + (cols[4] ? ' | ' + cols[4] : '');"
        "      out.push({ index: i, titulo: titulo, extracto: extracto, modo: 'selectiva' });"
        "    });"
        "    return out;"
        "  }"
        "  return [];"
        "}"
    )

    PAGINATION_JS = (
        "() => {"
        "  const all = Array.from(document.querySelectorAll('a, input[type=\"submit\"], input[type=\"button\"]'));"
        "  const nextEl = all.find(el => { const t = (el.textContent || el.value || el.title || '').trim(); return /^(siguiente|>>|>)$/i.test(t); });"
        "  const prevEl = all.find(el => { const t = (el.textContent || el.value || el.title || '').trim(); return /^(anterior|<<|<)$/i.test(t); });"
        "  const pageInfo = document.querySelector('.rf-ds-pg-cnt, [class*=\"pageCount\"], [class*=\"pageInfo\"], [class*=\"paginator\"]');"
        "  return { hasNext: !!(nextEl), hasPrev: !!(prevEl),"
        "    pageText: pageInfo ? pageInfo.textContent.trim().replace(/\\s+/g, ' ') : '' };"
        "}"
    )

    GO_PAGE_JS = (
        "(args) => {"
        "  const [textPats] = args;"
        "  const all = Array.from(document.querySelectorAll('a, input[type=\"submit\"], input[type=\"button\"]'));"
        "  for (const pat of textPats) {"
        "    const el = all.find(e => { const t = (e.textContent || e.value || e.title || '').trim().toLowerCase(); return t === pat; });"
        "    if (el) { el.click(); return true; }"
        "  }"
        "  return false;"
        "}"
    )

    def wait_results(page):
        try:
            page.wait_for_selector(
                'a[onclick*="lnkTituloSentencia"], td[id*="dataTable:"][id*=":colFec"]',
                timeout=20000
            )
        except Exception:
            pass

    def do_search(data):
        nonlocal sel_page
        modo          = data.get('modo', 'simple')
        texto         = data.get('texto', '').strip()
        tipo_busqueda = data.get('tipoBusqueda', 'TODAS_LAS_PALABRAS')
        ordenar       = data.get('ordenar', 'RELEVANCIA')
        fecha_desde   = data.get('fechaDesde', '').strip()
        fecha_hasta   = data.get('fechaHasta', '').strip()
        numero        = data.get('numeroSentencia', '').strip()
        procedimiento = data.get('procedimiento', '').strip()
        resumen       = data.get('resumen', '').strip()
        sede          = data.get('sede', '').strip()

        if modo == 'selectiva':
            page = get_sel_page()
            if page is None:
                raise RuntimeError('No se pudo inicializar la pagina de busqueda selectiva.')
            limpiar_formulario(page)
            fill_selectiva(page, texto, fecha_desde, fecha_hasta, numero, procedimiento, resumen, sede)
            wait_results(page)
            raw        = page.evaluate(EXTRACT_JS)
            pagination = page.evaluate(PAGINATION_JS)
            sid        = _save_session(page)
            # Preparar nueva pagina selectiva en background (via tarea en la cola)
            sel_page = None
            _task_queue.put({'type': 'warmup', 'result': None})
            return raw, sid, pagination
        else:
            page = new_ctx_page()
            fill_simple(page, texto, tipo_busqueda, ordenar)
            wait_results(page)
            raw        = page.evaluate(EXTRACT_JS)
            pagination = page.evaluate(PAGINATION_JS)
            sid        = _save_session(page)
            return raw, sid, pagination

    def do_pagina(sid, direction):
        s = _get_session(sid)
        if not s:
            raise ValueError('Sesion expirada.')
        page      = s['page']
        text_pats = ['siguiente', '>>', '>'] if direction == 'next' else ['anterior', '<<', '<']
        clicked   = page.evaluate(GO_PAGE_JS, [text_pats])
        if not clicked:
            raise ValueError('No hay mas paginas.')
        page.wait_for_timeout(2500)
        wait_results(page)
        raw        = page.evaluate(EXTRACT_JS)
        pagination = page.evaluate(PAGINATION_JS)
        return raw, sid, pagination

    def do_detalle(data):
        index = int(data.get('index', 0))
        sid   = data.get('sid', '')
        s     = _get_session(sid) if sid else None
        if s:
            page = s['page']
        else:
            raw, sid, _ = do_search(data)
            s    = _get_session(sid)
            page = s['page']

        links  = page.query_selector_all('a[onclick*="lnkTituloSentencia"]')
        celdas = page.query_selector_all('td[id*="dataTable:"][id*=":colFec"]')

        if links and index < len(links):
            titulo   = links[index].inner_text().strip()
            elemento = links[index]
        elif celdas and index < len(celdas):
            titulo   = page.evaluate('el => el.closest("tr").innerText.trim().substring(0, 120)', celdas[index])
            elemento = celdas[index]
        else:
            raise ValueError('Resultado no encontrado.')

        with page.context.expect_page(timeout=12000) as popup_info:
            elemento.click()
        popup = popup_info.value
        popup.wait_for_load_state('domcontentloaded', timeout=20000)
        detalle_text = popup.evaluate(
            "() => { const box = document.getElementById('textoSentenciaBox');"
            "  if (box) return box.innerText.trim(); return document.body.innerText.trim(); }"
        )
        popup_url = popup.url
        popup.close()
        return {'titulo': titulo, 'detalle': detalle_text, 'popup_url': popup_url, 'sid': sid}

    # Precalentar al arrancar
    print('[worker] Precalentando pagina selectiva...')
    load_selectiva()
    print('[worker] Pagina selectiva lista.')

    # Loop principal del worker
    while True:
        task = _task_queue.get()
        if task is None:
            break
        result_holder = task.get('result')  # threading.Event + dict compartido
        try:
            t = task['type']
            if t == 'warmup':
                load_selectiva()
            elif t == 'status':
                result_holder['value'] = {'selectiva_ready': sel_page is not None}
                result_holder['event'].set()
            elif t == 'search':
                raw, sid, pagination = do_search(task['data'])
                result_holder['value'] = {'raw': raw, 'sid': sid, 'pagination': pagination}
                result_holder['event'].set()
            elif t == 'pagina':
                raw, sid, pagination = do_pagina(task['sid'], task['direction'])
                result_holder['value'] = {'raw': raw, 'sid': sid, 'pagination': pagination}
                result_holder['event'].set()
            elif t == 'detalle':
                result_holder['value'] = do_detalle(task['data'])
                result_holder['event'].set()
        except Exception as e:
            if result_holder and 'event' in result_holder:
                result_holder['error'] = str(e)
                result_holder['event'].set()

def _call_worker(task_dict, timeout=28):
    """Envia una tarea al worker y espera el resultado."""
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

# ─── Cache de sesiones ────────────────────────────────────────────────────────

_sessions      = {}
_sessions_lock = threading.Lock()
SESSION_TTL    = 600

def _save_session(page) -> str:
    sid = str(uuid.uuid4())
    ctx = page.context
    with _sessions_lock:
        now   = time.time()
        stale = [k for k, v in _sessions.items() if now - v['ts'] > SESSION_TTL]
        for k in stale:
            try: _sessions[k]['ctx'].close()
            except: pass
            del _sessions[k]
        _sessions[sid] = {'page': page, 'ctx': ctx, 'ts': now}
    return sid

def _get_session(sid: str):
    with _sessions_lock:
        s = _sessions.get(sid)
        if s:
            s['ts'] = time.time()
        return s

def _close_session(sid: str):
    with _sessions_lock:
        s = _sessions.pop(sid, None)
    if s:
        try: s['ctx'].close()
        except: pass

# ─── Helpers de resultados ────────────────────────────────────────────────────

def parse_title(titulo: str) -> dict:
    m = re.match(r'^(\d+/\d+)\s+(\w+)\s+-\s+(.+?)\s+-\s+(.+)$', titulo)
    if m:
        return {'numero': m.group(1), 'tipo': m.group(2),
                'tribunal': m.group(3).strip(), 'proceso': m.group(4).strip()}
    return {'numero': '', 'tipo': '', 'tribunal': '', 'proceso': titulo}

def process_raw_results(raw: list) -> list:
    results = []
    for r in raw:
        if r.get('modo') == 'selectiva':
            parts = r['titulo'].split(' - ', 2)
            results.append({**r,
                'numero':   parts[0] if len(parts) > 0 else '',
                'tipo':     parts[1] if len(parts) > 1 else '',
                'tribunal': parts[2] if len(parts) > 2 else '',
                'proceso':  r['extracto'].split(' | ')[1] if ' | ' in r['extracto'] else ''})
        else:
            results.append({**r, **parse_title(r['titulo'])})
    return results

# ─── Rutas ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/api/buscar', methods=['POST'])
def buscar():
    data   = request.get_json() or {}
    campos = ['texto','fechaDesde','fechaHasta','numeroSentencia','procedimiento','resumen','sede']
    if not any(data.get(c, '').strip() for c in campos):
        return jsonify({'error': 'Ingrese al menos un criterio de busqueda.'}), 400
    try:
        res    = _call_worker({'type': 'search', 'data': data})
        results = process_raw_results(res['raw'])
        return jsonify({'results': results, 'total': len(results),
                        'query': data.get('texto', ''), 'sid': res['sid'],
                        'pagination': res['pagination']})
    except Exception as e:
        return jsonify({'error': f'Error al consultar el BJN: {str(e)}'}), 500

@app.route('/api/pagina', methods=['POST'])
def pagina():
    data      = request.get_json() or {}
    sid       = data.get('sid', '')
    direction = data.get('direction', 'next')
    if not sid:
        return jsonify({'error': 'Sesion no especificada.'}), 400
    try:
        res     = _call_worker({'type': 'pagina', 'sid': sid, 'direction': direction})
        results = process_raw_results(res['raw'])
        return jsonify({'results': results, 'total': len(results),
                        'sid': res['sid'], 'pagination': res['pagination']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/detalle', methods=['POST'])
def detalle():
    data = request.get_json() or {}
    try:
        res = _call_worker({'type': 'detalle', 'data': data})
        return jsonify(res)
    except Exception as e:
        return jsonify({'error': f'Error al cargar la sentencia: {str(e)}'}), 500

@app.route('/api/status', methods=['GET'])
def status():
    try:
        res = _call_worker({'type': 'status'}, timeout=3)
        return jsonify(res)
    except Exception:
        return jsonify({'selectiva_ready': False})

# ─── Arranque ─────────────────────────────────────────────────────────────────

# Iniciar el worker thread al importar el modulo
_worker_thread = threading.Thread(target=_playwright_worker, daemon=True, name='playwright-worker')
_worker_thread.start()

if __name__ == '__main__':
    print(f'\nBJN Buscador en http://localhost:{PORT}\n')
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
