"""
Extracción de citas legales uruguayas (leyes, decretos, jurisprudencia) desde texto libre.
Sin I/O: solo regex + normalización. Usado por server.py para el verificador de citas.
"""

import re

_NUM_ORD = r'N\s*(?:[°ºo]|[.]\s*[°º])\.?\s*'

# "Ley 19.355", "Ley Nº 18331", opcionalmente con año pegado "Ley 19.355/2015"
# (el plural de "ley" es "leyes", no "leys" — por eso "Ley(?:es)?" y no "Leyes?")
LEY_RE = re.compile(
    rf'\bLey(?:es)?\s*(?:{_NUM_ORD})?(\d{{1,2}}\.?\d{{3}})(?:\s*/\s*(\d{{4}}))?',
    re.IGNORECASE,
)

# "Decreto-Ley 14.500", "Decreto Ley Nº 15.365" — numerados como leyes, sin año en la cita
DECRETO_LEY_RE = re.compile(
    rf'\bDecreto[\s-]*Ley(?:es)?\s*(?:{_NUM_ORD})?(\d{{1,2}}\.?\d{{3}})',
    re.IGNORECASE,
)

# "Decreto 500/991", "Decreto Nº 152/013", "decreto 350/2019" — numero/año
DECRETO_RE = re.compile(
    rf'\bDecretos?\s*(?:{_NUM_ORD})?(\d{{1,4}})\s*/\s*(\d{{2,4}})',
    re.IGNORECASE,
)

# "Sentencia Nº 123/2020", "SEF 45/2021", "IUE 123-45/2020", "Fallo 1.234/2019"
JURISPRUDENCIA_RE = re.compile(
    rf'\b(?:Sentencia|SEF|IUE|Fallo)s?[\s-]*(?:{_NUM_ORD})?'
    rf'(\d{{1,4}}(?:[.-]\d{{1,6}})*)\s*/\s*(\d{{4}})',
    re.IGNORECASE,
)


def _normalizar_anio_corto(anio: str, anio_referencia: int) -> str:
    """
    Los decretos uruguayos suelen citarse con año corto (2-3 dígitos, ej. '013' = 2013,
    '991' = 1991). Se prueba primero el prefijo '20' y si da un año futuro o
    implausible se usa '19'. Heurística: sin acceso a IMPO en este entorno para
    confirmarla contra casos reales, revisar si aparecen falsos negativos en producción.
    """
    if len(anio) == 4:
        return anio
    candidato_2000 = int('20' + anio.zfill(2)[-2:]) if len(anio) == 2 else int('2' + anio)
    if 1900 <= candidato_2000 <= anio_referencia:
        return str(candidato_2000)
    candidato_1900 = int('19' + anio.zfill(2)[-2:]) if len(anio) == 2 else int('1' + anio)
    return str(candidato_1900)


def _normalizar_numero(numero: str) -> str:
    return numero.replace('.', '')


def extraer_citas(texto: str, anio_referencia: int = 2026) -> list[dict]:
    """
    Devuelve las citas legales encontradas en `texto`, en orden de aparición y sin
    duplicados (mismo tipo+numero+año). Cada cita: {cita, tipo, numero, anio, inicio}.
    `tipo` es uno de: 'ley', 'decreto_ley', 'decreto', 'jurisprudencia'.
    `anio` es None cuando el texto no permite determinarlo (ej. "Ley 19.355" sin año).
    """
    encontradas = []

    # "Decreto-Ley" contiene la subcadena "Ley", así que se resuelve primero y sus
    # tramos se excluyen de LEY_RE para no clasificar el mismo decreto-ley como ley.
    decreto_ley_spans = []
    for m in DECRETO_LEY_RE.finditer(texto):
        decreto_ley_spans.append(m.span())
        encontradas.append({
            'cita': m.group(0).strip(), 'tipo': 'decreto_ley',
            'numero': _normalizar_numero(m.group(1)),
            'anio': None, 'inicio': m.start(),
        })

    for m in LEY_RE.finditer(texto):
        if any(ini <= m.start() < fin for ini, fin in decreto_ley_spans):
            continue
        encontradas.append({
            'cita': m.group(0).strip(), 'tipo': 'ley',
            'numero': _normalizar_numero(m.group(1)),
            'anio': m.group(2), 'inicio': m.start(),
        })

    for m in DECRETO_RE.finditer(texto):
        encontradas.append({
            'cita': m.group(0).strip(), 'tipo': 'decreto',
            'numero': _normalizar_numero(m.group(1)),
            'anio': _normalizar_anio_corto(m.group(2), anio_referencia), 'inicio': m.start(),
        })

    for m in JURISPRUDENCIA_RE.finditer(texto):
        encontradas.append({
            'cita': m.group(0).strip(), 'tipo': 'jurisprudencia',
            'numero': _normalizar_numero(m.group(1)),
            'anio': m.group(2), 'inicio': m.start(),
        })

    encontradas.sort(key=lambda c: c['inicio'])

    vistos = set()
    resultado = []
    for c in encontradas:
        clave = (c['tipo'], c['numero'], c['anio'])
        if clave in vistos:
            continue
        vistos.add(clave)
        resultado.append(c)
    return resultado
