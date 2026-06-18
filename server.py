"""
BJN Buscador - Servidor Flask + Playwright
Busqueda de sentencias en la Base de Jurisprudencia Nacional (Uruguay)
v6: pagina selectiva permanente con boton Limpiar entre busquedas (~7s/busqueda).
    La carga inicial tarda ~60s (una vez al arrancar), luego cada busqueda es ~7s.
"""

from flask import Flask, request, jsonify, send_from_directory, Response
from playwright.sync_api import sync_playwright, TimeoutError as PwTimeout
import re, os, time, uuid, threading

app = Flask(__name__, static_folder='public')
PORT = int(os.environ.get('PORT', 3737))

BJN_SIMPLE = 'https://bjn.poderjudicial.gub.uy/BJNPUBLICA/busquedaSimple.seam'

# ─── Browser singleton ────────────────────────────────────────────────────────

_pw           = None
_browser      = None
_browser_lock = threading.Lock()

# Serializa todas las busquedas para evitar condiciones de carrera
_search_lock  = threading.Lock()

def get_browser():
    global _pw, _browser
    with _browser_lock:
        if _browser is None or not _browser.is_connected():
            if _pw:
                try: _pw.stop()
                except: pass
            _pw      = sync_playwright().start()
            _browser = _pw.chromium.launch(headless=True)
    return _browser

def new_page():
    global _browser
    for attempt in range(2):
        try:
            ctx  = get_browser().new_context(locale='es-UY')
            page = ctx.new_page()
            return page
        except Exception:
            if attempt == 0:
                with _browser_lock:
                    _browser = None
            else:
                raise

# ─── Pagina selectiva permanente ─────────────────────────────────────────────
# Mantiene una pagina con el formulario selectivo cargado de forma permanente.
# Flujo por busqueda:
#   1. Limpiar campos (boton Limpiar del BJN, ~5s)
#   2. Llenar campos con los criterios de busqueda
#   3. Clic en Buscar (~2s)
#   4. Extraer resultados
#   5. Guardar pagina en sesion para paginacion/detalle
#   6. Cargar nueva pagina selectiva en background para la proxima busqueda

_sel_page      = None
_sel_page_lock = threading.Lock()
_sel_warming   = False

def _load_selectiva_page() -> bool:
    """Carga el formulario selectivo. Devuelve True si tuvo exito."""
    global _sel_page
    try:
        page = new_page()
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
        with _sel_page_lock:
            _sel_page = page
        return True
    except Exception as e:
        print(f'[WARN] Error cargando pagina selectiva: {e}')
        return False

def _warm_selectiva_bg():
    global _sel_warming
    _sel_warming = True
    try:
        _load_selectiva_page()
    finally:
        _sel_warming = False

def _get_sel_page():
    """
    Devuelve la pagina selectiva. Si no esta lista, la carga (bloqueante).
    """
    global _sel_page
    with _sel_page_lock:
        page = _sel_page
    if page is not None:
        try:
            if 'busquedaSelectiva' in page.url:
                return page
        except Exception:
            pass
    # No esta lista: cargar bloqueante
    _load_selectiva_page()
    with _sel_page_lock:
        return _sel_page

def _limpiar_formulario(page) -> bool:
    """
    Usa el boton Limpiar del BJN para resetear el formulario.
    Devuelve True si tuvo exito.
    """
    try:
        limpiar_btn = page.locator(
            'input[value*="impiar"], a:has-text("Limpiar"), '
            'input[name*="Clear"], input[name*="clear"]'
        )
        if limpiar_btn.count() == 0:
            return False
        limpiar_btn.first.click()
        page.wait_for_timeout(2000)
        # Verificar que el formulario esta listo
        page.wait_for_selector('input[name*="ayudanteResumen"]', timeout=8000)
        return True
    except Exception:
        return False

# ─── Cache de sesiones ────────────────────────────────────────────────────────

_sessions      = {}
_sessions_lock = threading.Lock()
SESSION_TTL    = 600  # 10 minutos

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

# ─── Helpers Playwright ───────────────────────────────────────────────────────

def parse_title(titulo: str) -> dict:
    m = re.match(r'^(\d+/\d+)\s+(\w+)\s+-\s+(.+?)\s+-\s+(.+)$', titulo)
    if m:
        return {'numero': m.group(1), 'tipo': m.group(2),
                'tribunal': m.group(3).strip(), 'proceso': m.group(4).strip()}
    return {'numero': '', 'tipo': '', 'tribunal': '', 'proceso': titulo}

def _fill_verified(page, selector: str, value: str, max_attempts: int = 3) -> bool:
    """Llena un campo y verifica que el valor quedo escrito. Reintenta si es necesario."""
    for attempt in range(max_attempts):
        el = page.locator(selector)
        if el.count() == 0:
            return False
        el.first.fill(value)
        page.wait_for_timeout(300)
        actual = el.first.input_value()
        if actual == value:
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

def fill_selectiva(page, texto, fecha_desde, fecha_hasta, numero, procedimiento, resumen, sede=''):
    """
    Llena el formulario selectivo. La pagina ya esta en busquedaSelectiva.seam
    con el formulario limpio y listo.
    """
    # Texto libre (textarea)
    if texto:
        filled = False
        for sel in ['textarea[name*="cajaQuery"]', 'textarea[name*="query"]',
                    'textarea[name*="Query"]']:
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
        _fill_verified(page, 'input[name*="fechaDesdeCalInputDate"]', fecha_desde)
        page.locator('input[name*="fechaDesdeCalInputDate"]').first.press('Tab')
        page.wait_for_timeout(1500)

    if fecha_hasta:
        _fill_verified(page, 'input[name*="fechaHastaCalInputDate"]', fecha_hasta)
        page.locator('input[name*="fechaHastaCalInputDate"]').first.press('Tab')
        page.wait_for_timeout(1500)

    if numero:
        _fill_verified(page, 'input[name*="ayudanteNumero"]', numero)

    if procedimiento:
        _fill_verified(page, 'input[name*="ayudanteProc"]', procedimiento)

    if resumen:
        _fill_verified(page, 'input[name*="ayudanteResumen"]', resumen)

    if sede:
        for sel in ['input[name*="ayudanteSede"]', 'input[name*="Sede"]',
                    'input[name*="sede"]', 'input[name*="tribunal"]']:
            try:
                el = page.locator(sel)
                if el.count() > 0:
                    _fill_verified(page, sel, sede)
                    break
            except Exception:
                pass

    page.locator('input[name="formBusqueda:j_id20:Search"]').click()

def wait_results(page):
    try:
        page.wait_for_selector(
            'a[onclick*="lnkTituloSentencia"], td[id*="dataTable:"][id*=":colFec"]',
            timeout=20000
        )
    except PwTimeout:
        pass

_EXTRACT_JS = (
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

_PAGINATION_JS = (
    "() => {"
    "  const all = Array.from(document.querySelectorAll('a, input[type=\"submit\"], input[type=\"button\"]'));"
    "  const nextEl = all.find(el => { const t = (el.textContent || el.value || el.title || '').trim(); return /^(siguiente|>>|>)$/i.test(t); });"
    "  const prevEl = all.find(el => { const t = (el.textContent || el.value || el.title || '').trim(); return /^(anterior|<<|<)$/i.test(t); });"
    "  const pageInfo = document.querySelector('.rf-ds-pg-cnt, [class*=\"pageCount\"], [class*=\"pageInfo\"], [class*=\"paginator\"]');"
    "  return {"
    "    hasNext: !!(nextEl),"
    "    hasPrev: !!(prevEl),"
    "    pageText: pageInfo ? pageInfo.textContent.trim().replace(/\\s+/g, ' ') : ''"
    "  };"
    "}"
)

_GO_PAGE_JS = (
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

def extract_results(page) -> list:
    return page.evaluate(_EXTRACT_JS)

def check_pagination(page) -> dict:
    return page.evaluate(_PAGINATION_JS)

def go_page(page, direction: str) -> bool:
    text_pats = ['siguiente', '>>', '>'] if direction == 'next' else ['anterior', '<<', '<']
    clicked   = page.evaluate(_GO_PAGE_JS, [text_pats])
    if clicked:
        page.wait_for_timeout(2500)
        wait_results(page)
    return clicked

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

def do_search(data: dict):
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
        # 1. Obtener la pagina precalentada
        sel_page = _get_sel_page()
        if sel_page is None:
            raise RuntimeError('No se pudo inicializar la pagina de busqueda selectiva.')

        # 2. Limpiar el formulario con el boton Limpiar del BJN
        _limpiar_formulario(sel_page)

        # 3. Llenar el formulario y buscar
        fill_selectiva(sel_page, texto, fecha_desde, fecha_hasta,
                       numero, procedimiento, resumen, sede)
        wait_results(sel_page)

        # 4. Extraer resultados y paginacion
        raw        = extract_results(sel_page)
        results    = process_raw_results(raw)
        pagination = check_pagination(sel_page)

        # 5. Guardar la pagina en una sesion para paginacion/detalle
        sid = _save_session(sel_page)

        # 6. Marcar _sel_page como None y cargar nueva en background
        with _sel_page_lock:
            _sel_page = None
        threading.Thread(target=_warm_selectiva_bg, daemon=True).start()

        return results, sid, pagination
    else:
        page = new_page()
        fill_simple(page, texto, tipo_busqueda, ordenar)
        wait_results(page)
        raw        = extract_results(page)
        results    = process_raw_results(raw)
        pagination = check_pagination(page)
        sid        = _save_session(page)
        return results, sid, pagination

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
    with _search_lock:
        try:
            results, sid, pagination = do_search(data)
            return jsonify({'results': results, 'total': len(results),
                            'query': data.get('texto', ''), 'sid': sid, 'pagination': pagination})
        except Exception as e:
            return jsonify({'error': f'Error al consultar el BJN: {str(e)}'}), 500

@app.route('/api/pagina', methods=['POST'])
def pagina():
    data      = request.get_json() or {}
    sid       = data.get('sid', '')
    direction = data.get('direction', 'next')
    session   = _get_session(sid) if sid else None
    if not session:
        return jsonify({'error': 'Sesion expirada. Realice la busqueda nuevamente.'}), 400
    page = session['page']
    with _search_lock:
        try:
            ok = go_page(page, direction)
            if not ok:
                return jsonify({'error': 'No hay mas paginas en esa direccion.'}), 400
            raw        = extract_results(page)
            results    = process_raw_results(raw)
            pagination = check_pagination(page)
            return jsonify({'results': results, 'total': len(results),
                            'sid': sid, 'pagination': pagination})
        except Exception as e:
            return jsonify({'error': str(e)}), 500

@app.route('/api/detalle', methods=['POST'])
def detalle():
    data  = request.get_json() or {}
    index = int(data.get('index', 0))
    sid   = data.get('sid', '')

    with _search_lock:
        try:
            session = _get_session(sid) if sid else None
            if session:
                page = session['page']
            else:
                _, sid, _ = do_search(data)
                session   = _get_session(sid)
                page      = session['page']

            links  = page.query_selector_all('a[onclick*="lnkTituloSentencia"]')
            celdas = page.query_selector_all('td[id*="dataTable:"][id*=":colFec"]')

            if links and index < len(links):
                titulo   = links[index].inner_text().strip()
                elemento = links[index]
            elif celdas and index < len(celdas):
                tr     = page.evaluate('el => el.closest("tr").innerText.trim().substring(0, 120)', celdas[index])
                titulo = tr
                elemento = celdas[index]
            else:
                return jsonify({'error': 'Resultado no encontrado.'}), 404

            with page.context.expect_page(timeout=12000) as popup_info:
                elemento.click()

            popup = popup_info.value
            popup.wait_for_load_state('domcontentloaded', timeout=20000)

            detalle_text = popup.evaluate(
                "() => { const box = document.getElementById('textoSentenciaBox');"
                "  if (box) return box.innerText.trim();"
                "  return document.body.innerText.trim(); }"
            )

            popup_url = popup.url
            popup.close()

            return jsonify({'titulo': titulo, 'detalle': detalle_text,
                            'popup_url': popup_url, 'sid': sid})

        except PwTimeout:
            return jsonify({'error': 'El BJN tardo demasiado en responder. Intente nuevamente.'}), 504
        except Exception as e:
            _close_session(sid)
            return jsonify({'error': f'Error al cargar la sentencia: {str(e)}'}), 500

@app.route('/api/ver-sentencia', methods=['POST'])
def ver_sentencia():
    data      = request.get_json() or {}
    popup_url = data.get('popup_url', '')
    if not popup_url or 'bjn.poderjudicial' not in popup_url:
        return jsonify({'error': 'URL no valida.'}), 400
    try:
        page = new_page()
        page.goto(popup_url, wait_until='domcontentloaded', timeout=15000)
        html = page.evaluate(
            "() => { const box = document.getElementById('textoSentenciaBox');"
            "  return box ? box.innerHTML : document.body.innerHTML; }"
        )
        page.context.close()
        return Response(html, mimetype='text/html')
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/status', methods=['GET'])
def status():
    with _sel_page_lock:
        ready = _sel_page is not None
    return jsonify({'selectiva_ready': ready, 'warming': _sel_warming})

import atexit

@atexit.register
def _shutdown():
    global _pw, _browser, _sel_page
    for s in list(_sessions.values()):
        try: s['ctx'].close()
        except: pass
    if _sel_page:
        try: _sel_page.context.close()
        except: pass
    if _browser:
        try: _browser.close()
        except: pass
    if _pw:
        try: _pw.stop()
        except: pass

if __name__ == '__main__':
    print(f'\nBJN Buscador en http://localhost:{PORT}\n')
    print('Precalentando pagina selectiva del BJN...')
    threading.Thread(target=_warm_selectiva_bg, daemon=True).start()
    app.run(host='0.0.0.0', port=PORT, debug=False, threaded=True)
