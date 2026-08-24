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
import re, os, uuid, threading, queue, time, unicodedata, html, subprocess
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import parse_qs, quote_plus, unquote, urlencode, urlparse
import httpx
from bs4 import BeautifulSoup
from citations import extraer_citas

app = Flask(__name__, static_folder='public')
PORT = int(os.environ.get('PORT', 3737))

BJN_SIMPLE = 'https://bjn.poderjudicial.gub.uy/BJNPUBLICA/busquedaSimple.seam'
IMPO_BASE = 'https://impo.com.uy/bases'
IMPO_TIPO_PATH = {'ley': 'leyes', 'decreto': 'decretos', 'decreto_ley': 'decretos-ley'}

# ─── Job store ────────────────────────────────────────────────────────────────
JOB_TTL_SECONDS = int(os.environ.get('JOB_TTL_SECONDS', '300'))
_jobs      = {}
_jobs_lock = threading.Lock()


def _purge_jobs():
    """Elimina jobs vencidos para que una instancia de Render no crezca sin límite."""
    now = time.monotonic()
    with _jobs_lock:
        expired = []
        for jid, job in _jobs.items():
            reference = job.get('completed_at') or job.get('created_at', now)
            if now - reference > JOB_TTL_SECONDS:
                expired.append(jid)
        for jid in expired:
            _jobs.pop(jid, None)


def _new_job():
    _purge_jobs()
    jid = str(uuid.uuid4())
    with _jobs_lock:
        _jobs[jid] = {
            'status': 'pending',
            'result': None,
            'error': None,
            'created_at': time.monotonic(),
        }
    return jid


def _finish_job(jid, result=None, error=None):
    with _jobs_lock:
        if jid in _jobs:
            _jobs[jid]['status'] = 'done' if result is not None else 'error'
            _jobs[jid]['result'] = result
            _jobs[jid]['error']  = error
            _jobs[jid]['completed_at'] = time.monotonic()

# ─── Worker thread dedicado para Playwright ───────────────────────────────────
_task_queue = queue.Queue()

_state = {
    'page': None,
    'ctx':  None,
    'ready': False,
    'error': None,
}
_state_lock = threading.Lock()

def _playwright_worker():
    from playwright.sync_api import sync_playwright, TimeoutError as PWTimeout

    try:
        pw      = sync_playwright().start()
        browser = pw.chromium.launch(headless=True)
        ctx     = browser.new_context(locale='es-UY')
        page    = ctx.new_page()
    except Exception as exc:
        message = f'No se pudo iniciar el motor de consulta: {exc}'
        with _state_lock:
            _state['ready'] = False
            _state['error'] = message
        # Completar como error las tareas que hayan llegado mientras iniciaba.
        while True:
            task = _task_queue.get()
            if task is None:
                return
            _finish_job(task.get('jid'), error=message)

    with _state_lock:
        _state['page'] = page
        _state['ctx']  = ctx
        _state['ready'] = True
        _state['error'] = None

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

    PAGINATION_JS = r"""() => {
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
        sinonimos     = bool(data.get('sinonimos', False))

        # La verificación usa un timeout menor para que una cita no localizada no bloquee todo el job.
        verify_mode = bool(data.get('_verificacion_bjn'))
        navigation_timeout = 25000 if verify_mode else 40000
        try:
            page.goto(BJN_SIMPLE, wait_until='domcontentloaded', timeout=navigation_timeout)
        except PWTimeout:
            page.goto(BJN_SIMPLE, wait_until='commit', timeout=15000)
        selector_timeout = 18000 if verify_mode else 15000
        try:
            page.wait_for_selector('#formBusqueda\\:cajaQuery', timeout=selector_timeout)
        except PWTimeout:
            page.goto(BJN_SIMPLE, wait_until='domcontentloaded', timeout=25000 if verify_mode else 40000)
            page.wait_for_selector('#formBusqueda\\:cajaQuery', timeout=selector_timeout)

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

        # El selector oficial de sinónimos aparece dentro de las opciones avanzadas.
        try:
            page.locator('#formBusqueda\\:decSinonimos\\:chkSinonimos').set_checked(
                sinonimos, timeout=5000)
        except Exception:
            pass

        page.click('#formBusqueda\\:Search')
        wait_results(15000 if verify_mode else 25000)

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

    BJN_VERIFY_MAX_PAGES = int(os.environ.get('BJN_VERIFY_MAX_PAGES', '3'))

    def _bjn_query_variants(cita):
        """Construye consultas canónicas sin perder el identificador original."""
        numero = str(cita.get('numero') or '').strip()
        anio = str(cita.get('anio') or '').strip()
        base = f'{numero}/{anio}'
        variantes = [base]
        if '-' in numero:
            partes = numero.split('-')
            padded = '-'.join(
                parte.zfill(4 if indice == 0 else 6)
                for indice, parte in enumerate(partes)
            )
            variantes.append(f'{padded}/{anio}')
        prefijo = str(cita.get('prefijo') or '').strip()
        if prefijo and prefijo not in {'SENTENCIA', 'FALLO'}:
            variantes.append(f'{prefijo} {base}')
        return list(dict.fromkeys(variantes))

    def _bjn_result_matches(cita, resultado):
        objetivo = _normalizar_identificador(f"{cita['numero']}/{cita['anio']}")
        titulo = resultado.get('titulo', '')
        if not (objetivo in _normalizar_identificador(titulo)
                or _normalizar_identificador(resultado.get('numero', '')) == objetivo):
            return False
        # El BJN no siempre incluye el acrónimo del tribunal en el título indexado.
        # El número/año es la coincidencia verificable; el prefijo original se conserva.
        return True

    def _buscar_bjn_identificador(cita):
        """Busca en las primeras páginas y devuelve la primera coincidencia exacta."""
        for query in _bjn_query_variants(cita):
            raw, pagination = do_search({
                'texto': query,
                'tipoBusqueda': 'FRASE_EXACTA',
                'ordenar': 'RELEVANCIA',
                '_verificacion_bjn': True,
            })
            for page_number in range(BJN_VERIFY_MAX_PAGES + 1):
                coincidencia = next(
                    (
                        resultado for resultado in process_raw_results(raw)
                        if _bjn_result_matches(cita, resultado)
                    ),
                    None,
                )
                if coincidencia:
                    return coincidencia, query
                if not pagination.get('hasNext') or page_number >= BJN_VERIFY_MAX_PAGES:
                    break
                raw, pagination = do_pagina('next')
        return None, None

    def verificar_jurisprudencia(cita):
        """Busca la sentencia por identificador canónico y revisa páginas adicionales."""
        if not cita.get('anio') or not cita.get('numero'):
            return {**cita, 'fuente': 'BJN', 'estado': 'no_verificable',
                    'detalle': 'La referencia no contiene número y año suficientes para consultar el BJN.',
                    'url': BJN_SIMPLE}
        try:
            coincidencia, query = _buscar_bjn_identificador(cita)
        except Exception as exc:
            return {**cita, 'fuente': 'BJN', 'estado': 'no_verificable',
                    'detalle': f'No se pudo consultar el BJN: {exc}', 'url': BJN_SIMPLE}
        if coincidencia:
            return {**cita, 'fuente': 'BJN', 'estado': 'verificada',
                    'detalle': coincidencia.get('titulo', ''), 'url': BJN_SIMPLE,
                    'consulta_bjn': query}
        return {**cita, 'fuente': 'BJN', 'estado': 'no_encontrada',
                'detalle': 'No se encontró una sentencia con ese identificador en las páginas consultadas del BJN.',
                'url': BJN_SIMPLE}

    def do_verificar(texto):
        citas = extraer_citas(texto)
        resultados = []
        for cita in citas:
            if cita['tipo'] == 'jurisprudencia':
                resultados.append(verificar_jurisprudencia(cita))
            else:
                resultados.append(verificar_impo(cita))
        return resultados

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
            elif t == 'verificar':
                citas = do_verificar(task['texto'])
                _finish_job(jid, result={'citas': citas, 'total': len(citas)})
        except Exception as e:
            _finish_job(jid, error=str(e))


# ─── Helpers de resultados ────────────────────────────────────────────────────

def parse_title(titulo: str) -> dict:
    """Descompone títulos numéricos y títulos SEF conservando el identificador completo."""
    m = re.match(
        r'^(?P<numero>(?:(?:SEF|DFA|IUE)\s+)?'
        r'(?:\d[\d.-]*|[A-Z][A-Z0-9-]*)/\d{4})\s+'
        r'(?P<tipo>DEFINITIVA|INTERLOCUTORIA)\s+-\s+(?P<resto>.+)$',
        titulo or '', re.IGNORECASE,
    )
    if m:
        tribunal, sep, proceso = m.group('resto').partition(' - ')
        return {
            'numero': m.group('numero').strip(),
            'tipo': m.group('tipo').strip(),
            'tribunal': tribunal.strip(),
            'proceso': proceso.strip() if sep else '',
        }
    return {'numero': '', 'tipo': '', 'tribunal': '', 'proceso': titulo}

def process_raw_results(raw: list) -> list:
    return [{**r, **parse_title(r['titulo'])} for r in raw]


def _normalizar_identificador(value: str) -> str:
    """Normaliza identificadores para comparar citas sin depender de puntos o guiones."""
    normalized = unicodedata.normalize('NFKD', value or '')
    normalized = ''.join(ch for ch in normalized if not unicodedata.combining(ch))
    return re.sub(r'[^a-z0-9]', '', normalized.lower())


_IMPO_RESOLVE_CACHE = {}
_IMPO_RESOLVE_CACHE_TTL = int(os.environ.get('IMPO_RESOLVE_CACHE_TTL', '21600'))
_IMPO_RESOLVE_LOCK = threading.Lock()
_IMPO_RESOLVE_CHUNK = 18
_IMPO_MIN_YEAR = 1830
_IMPO_MAX_YEAR = time.gmtime().tm_year


def _impo_years_to_try(numero: str) -> list[int]:
    """Ordena los años probables por la numeración histórica de las leyes uruguayas."""
    digits = re.sub(r'\D', '', str(numero or ''))
    try:
        value = int(digits)
    except ValueError:
        value = 0
    if value >= 18000:
        inicio = 1995
    elif value >= 16000:
        inicio = 1980
    elif value >= 14000:
        inicio = 1965
    elif value >= 12000:
        inicio = 1945
    elif value >= 10000:
        inicio = 1925
    elif value >= 8000:
        inicio = 1905
    else:
        inicio = _IMPO_MIN_YEAR
    primarios = list(range(_IMPO_MAX_YEAR, max(_IMPO_MIN_YEAR, inicio) - 1, -1))
    secundarios = list(range(min(_IMPO_MAX_YEAR, inicio - 1), _IMPO_MIN_YEAR - 1, -1))
    return primarios + secundarios


def _impo_path(cita: dict, anio: str) -> str:
    tipo_path = IMPO_TIPO_PATH.get(cita['tipo'])
    return f"{IMPO_BASE}/{tipo_path}/{cita['numero']}-{anio}"


def _impo_content_matches(content: str, cita: dict) -> bool:
    """Comprueba el título y el número para no confundir la pantalla de ingreso con una norma."""
    content = content[:12000]
    if re.search(r'<title[^>]*>\s*(?:Ingreso|Página no encontrada)', content, re.IGNORECASE):
        return False
    number = re.sub(r'[^0-9]', '', str(cita.get('numero') or ''))
    compact = re.sub(r'[^a-z0-9]', '', content.lower())
    if not number or number not in compact:
        return False
    if cita.get('tipo') == 'ley':
        return bool(re.search(r'\bley\b', content, re.IGNORECASE))
    if cita.get('tipo') == 'decreto_ley':
        return bool(re.search(r'decreto[\s-]*ley', content, re.IGNORECASE))
    return bool(re.search(r'\bdecreto\b', content, re.IGNORECASE))


def _impo_page_matches(response: httpx.Response, cita: dict) -> bool:
    return response.status_code == 200 and _impo_content_matches(response.text, cita)


def _impo_search_queries(cita: dict) -> list[str]:
    tipo = cita.get('tipo')
    numero = cita.get('numero', '')
    if tipo == 'ley':
        return [
            f'site:impo.com.uy/bases/leyes {numero}',
            f'site:impo.com.uy/bases/leyes "Ley N° {numero}"',
        ]
    if tipo == 'decreto_ley':
        return [
            f'site:impo.com.uy/bases/decretos-ley {numero}',
            f'site:impo.com.uy/bases/decretos-ley "Decreto-Ley N° {numero}"',
        ]
    return [
        f'site:impo.com.uy/bases/decretos {numero}',
        f'site:impo.com.uy/bases/decretos "Decreto N° {numero}"',
        f'site:impo.com.uy/bases/decretos {numero}/',
        f'site:impo.com.uy/bases/decretos "Decreto N° {numero}/"',
        f'site:impo.com.uy/bases/decretos "Decreto {numero}/"',
    ]


def _curl_get(url: str, timeout: int = 20):
    """Obtiene una URL con curl y devuelve un Response compatible con httpx."""
    marker = b'\n__BJN_STATUS__'
    try:
        completed = subprocess.run(
            [
                'curl', '-4', '-LsS', '--retry', '2', '--retry-all-errors', '--retry-delay', '1',
                '--max-time', str(timeout), '--connect-timeout', '8',
                '-A', 'BJN-Buscador/1.0', '-w', '\n__BJN_STATUS__%{http_code}', url,
            ],
            capture_output=True,
            timeout=timeout + 3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    body, separator, status_text = completed.stdout.rpartition(marker)
    if not separator:
        return None
    try:
        status = int(status_text.decode('ascii', errors='ignore').strip())
    except ValueError:
        return None
    return httpx.Response(status, content=body, request=httpx.Request('GET', url))


def _impo_search_links(cita: dict) -> list[dict]:
    """Lee el RSS público de Bing y devuelve solo URLs canónicas de IMPO."""
    path_prefix = {
        'ley': '/bases/leyes/',
        'decreto': '/bases/decretos/',
        'decreto_ley': '/bases/decretos-ley/',
    }.get(cita.get('tipo'), '')
    number = re.sub(r'\D', '', str(cita.get('numero') or ''))
    links = []
    for search_query in _impo_search_queries(cita):
        query = quote_plus(search_query)
        endpoints = [
            f'https://www.bing.com/search?format=rss&q={query}',
            f'https://www.bing.com/search?q={query}',
        ]
        for endpoint in endpoints:
            response = _curl_get(endpoint, timeout=20)
            if response is None or response.status_code != 200:
                continue
            raw_links = re.findall(r'<link>(https?://[^<]+)</link>', response.text, re.IGNORECASE)
            raw_links.extend(re.findall(r'href=["\'](https?://[^"\']+)["\']', response.text, re.IGNORECASE))
            for raw_link in raw_links:
                candidate = html.unescape(raw_link)
                parsed = urlparse(candidate)
                if parsed.netloc.lower() not in {'www.impo.com.uy', 'impo.com.uy'}:
                    continue
                match = re.match(re.escape(path_prefix) + r'(\d+)-(\d{4})$', parsed.path)
                if not match or match.group(1) != number:
                    continue
                item = {'anio': match.group(2), 'url': f'https://www.impo.com.uy{parsed.path}'}
                if item not in links:
                    links.append(item)
            if links:
                break
        if links:
            break
    return links


def _impo_request(url: str, params: dict, timeout: httpx.Timeout):
    """Consulta IMPO con curl y prueba HTTPX como respaldo."""
    query = urlencode(params or {})
    candidatos = [url, url.replace('https://www.impo.com.uy', 'https://impo.com.uy')]
    for candidato in dict.fromkeys(candidatos):
        target = f'{candidato}?{query}' if query else candidato
        response = _curl_get(target, timeout=20)
        if response is not None and response.status_code == 200:
            return response
    for candidato in dict.fromkeys(candidatos):
        try:
            return httpx.get(
                candidato,
                params=params,
                timeout=timeout,
                follow_redirects=True,
                headers={'User-Agent': 'BJN-Buscador/1.0'},
            )
        except httpx.HTTPError:
            continue
    return None


def _impo_get_candidate(cita: dict, anio: int):
    url = _impo_path(cita, str(anio))
    response = _impo_request(
        url,
        params={'json': 'true'},
        timeout=httpx.Timeout(8.0, connect=4.0),
    )
    if response is not None and _impo_page_matches(response, cita):
        return {'anio': str(anio), 'url': url}
    return None


def _resolver_impo_por_numero(cita: dict) -> list[dict]:
    """Resuelve el año de una norma sin año mediante páginas públicas de IMPO.

    La búsqueda se hace en bloques recientes y se detiene en el primer bloque que
    contenga coincidencias. La respuesta solo se considera positiva si la página
    devuelve el tipo y el número de la norma, no solo un HTTP 200.
    """
    cache_key = (cita.get('tipo'), cita.get('numero'))
    now = time.monotonic()
    with _IMPO_RESOLVE_LOCK:
        cached = _IMPO_RESOLVE_CACHE.get(cache_key)
        if cached and now - cached['at'] < _IMPO_RESOLVE_CACHE_TTL:
            return list(cached['matches'])

    matches = _impo_search_links(cita)
    if matches:
        with _IMPO_RESOLVE_LOCK:
            _IMPO_RESOLVE_CACHE[cache_key] = {'at': time.monotonic(), 'matches': list(matches)}
        return matches

    years = _impo_years_to_try(cita.get('numero'))
    matches = []
    for start in range(0, len(years), _IMPO_RESOLVE_CHUNK):
        chunk = years[start:start + _IMPO_RESOLVE_CHUNK]
        with ThreadPoolExecutor(max_workers=9) as executor:
            futures = [executor.submit(_impo_get_candidate, cita, anio) for anio in chunk]
            for future in futures:
                match = future.result()
                if match:
                    matches.append(match)
        if matches:
            break

    with _IMPO_RESOLVE_LOCK:
        _IMPO_RESOLVE_CACHE[cache_key] = {'at': time.monotonic(), 'matches': list(matches)}
    return matches


def verificar_impo(cita: dict) -> dict:
    """Consulta IMPO y resuelve el año cuando la cita no lo trae."""
    if cita.get('anio'):
        url = _impo_path(cita, cita['anio'])
        try:
            response = _impo_request(
                url,
                params={'json': 'true'},
                timeout=httpx.Timeout(10.0, connect=5.0),
            )
            if response is None:
                raise httpx.ConnectError('No se pudo establecer conexión con IMPO')
        except httpx.HTTPError as exc:
            return {**cita, 'fuente': 'IMPO', 'estado': 'no_verificable',
                    'detalle': f'No se pudo consultar IMPO: {exc}', 'url': url}
        if _impo_page_matches(response, cita):
            return {**cita, 'fuente': 'IMPO', 'estado': 'verificada',
                    'detalle': 'Norma encontrada en IMPO.', 'url': url}
        return {**cita, 'fuente': 'IMPO', 'estado': 'no_encontrada',
                'detalle': 'IMPO no tiene registrada una norma con ese número y año.', 'url': url}

    matches = _resolver_impo_por_numero(cita)
    if len(matches) == 1 and cita.get('tipo') == 'decreto':
        resolved = matches[0]
        return {**cita, 'anio': resolved['anio'], 'fuente': 'IMPO',
                'estado': 'no_verificable',
                'detalle': f"Se localizó el Decreto {cita.get('numero')} en IMPO para {resolved['anio']}, pero falta el año en la cita y el número puede repetirse.",
                'url': resolved['url']}
    if len(matches) == 1:
        resolved = matches[0]
        return {**cita, 'anio': resolved['anio'], 'fuente': 'IMPO',
                'estado': 'verificada',
                'detalle': f"Norma encontrada en IMPO. Año resuelto por número: {resolved['anio']}.",
                'url': resolved['url']}
    if len(matches) > 1:
        years = ', '.join(match['anio'] for match in matches)
        return {**cita, 'fuente': 'IMPO', 'estado': 'no_verificable',
                'detalle': f'El número aparece en más de una norma de IMPO ({years}); falta el año en la cita.',
                'url': matches[0]['url']}
    return {**cita, 'fuente': 'IMPO', 'estado': 'no_encontrada',
            'detalle': 'No se localizó una norma pública de IMPO con ese tipo y número.',
            'url': None}


# ─── Rutas ────────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/verificador')
def verificador_page():
    return send_from_directory('public', 'verificador.html')

@app.route('/api/job/<jid>', methods=['GET'])
def get_job(jid):
    _purge_jobs()
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
    if direction not in ('next', 'prev'):
        return jsonify({'error': "La dirección debe ser 'next' o 'prev'."}), 400
    jid = _submit_job({'type': 'pagina', 'direction': direction})
    return jsonify({'job_id': jid})

@app.route('/api/detalle', methods=['POST'])
def detalle():
    data = request.get_json() or {}
    try:
        index = int(data.get('index', 0))
    except (TypeError, ValueError):
        return jsonify({'error': 'El índice de la sentencia no es válido.'}), 400
    if index < 0:
        return jsonify({'error': 'El índice de la sentencia no es válido.'}), 400
    jid = _submit_job({'type': 'detalle', 'index': index})
    return jsonify({'job_id': jid})

@app.route('/api/verificar', methods=['POST'])
def verificar():
    data  = request.get_json() or {}
    texto = data.get('texto', '').strip()
    if not texto:
        return jsonify({'error': 'Ingrese un texto para verificar.'}), 400
    jid = _submit_job({'type': 'verificar', 'texto': texto})
    return jsonify({'job_id': jid})

@app.route('/healthz', methods=['GET'])
def healthz():
    """Health check del proceso; incluye la disponibilidad del worker de Playwright."""
    with _state_lock:
        ready = _state['ready']
        error = _state['error']
    return jsonify({'ok': True, 'worker_ready': ready, 'worker_error': error})


@app.route('/api/status', methods=['GET'])
def status():
    with _state_lock:
        ready = _state['ready']
        error = _state['error']
    return jsonify({'ok': ready, 'worker_ready': ready, 'error': error})


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
        'Usa navegar_pagina con next o prev para paginar resultados. '
        'Usa verificar_texto para detectar citas legales (leyes, decretos, jurisprudencia) '
        'alucinadas por IA, verificándolas contra IMPO y el BJN.'
    ),
)

_LOCAL = f'http://127.0.0.1:{PORT}'

_TIPO_MAP = {
    'todas': 'TODAS_LAS_PALABRAS',
    'frase': 'FRASE_EXACTA',
    'alguna': 'ALGUNA_PALABRA',
    'maximizar': 'MAXIMIZAR_RESULTADOS',
}
_ORDEN_MAP = {
    'relevancia': 'RELEVANCIA',
    'fecha': 'FECHA_DESCENDENTE',
    'fecha_descendente': 'FECHA_DESCENDENTE',
    'fecha_ascendente': 'FECHA_ASCENDENTE',
}


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
def buscar_jurisprudencia(texto: str, modo: str = 'todas', orden: str = 'relevancia', sinonimos: bool = False) -> str:
    """
    Busca sentencias en la Base de Jurisprudencia Nacional (BJN) de Uruguay.

    Args:
        texto: Palabras clave o frase a buscar.
        modo: 'todas' (todas las palabras), 'frase' (frase exacta) o 'alguna' (alguna palabra).
        orden: 'relevancia', 'fecha', 'fecha_descendente' o 'fecha_ascendente'.
        sinonimos: si es true, habilita los sinónimos del BJN.

    Returns:
        Lista de sentencias con su index, número, tribunal y extracto.
    """
    payload = {
        'texto': texto,
        'tipoBusqueda': _TIPO_MAP.get(modo, 'TODAS_LAS_PALABRAS'),
        'ordenar': _ORDEN_MAP.get(orden, 'RELEVANCIA'),
        'sinonimos': bool(sinonimos),
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


_ESTADO_ICONO = {'verificada': '✅', 'no_encontrada': '❌', 'no_verificable': '⚠️'}


def _fmt_citas(citas: list) -> str:
    lines = []
    for c in citas:
        icono = _ESTADO_ICONO.get(c['estado'], '?')
        lines.append(
            f"{icono} **{c['cita']}** ({c['fuente']}) — {c['estado']}\n{c.get('detalle', '')}"
            + (f"\n{c['url']}" if c.get('url') else '')
        )
    return '\n\n'.join(lines)


@_mcp.tool()
def verificar_texto(texto: str) -> str:
    """
    Extrae citas legales uruguayas (leyes, decretos, jurisprudencia) de un texto y
    verifica contra fuentes oficiales (IMPO, BJN) si existen realmente. Sirve para
    detectar citas alucinadas por IA en escritos, dictámenes o resoluciones.

    Args:
        texto: Texto a analizar en busca de citas legales.

    Returns:
        Por cada cita: si fue verificada, no encontrada, o no se pudo verificar, con enlace.
    """
    r = _httpx.post(f'{_LOCAL}/api/verificar', json={'texto': texto}, timeout=30)
    r.raise_for_status()
    data = _poll_local(r.json()['job_id'], timeout=90)
    if data.get('status') == 'error':
        return f"Error: {data.get('error', 'desconocido')}"
    citas = data.get('citas', [])
    if not citas:
        return 'No se encontraron citas legales reconocibles en el texto.'
    return f"Se encontraron {len(citas)} cita(s):\n\n" + _fmt_citas(citas)


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
