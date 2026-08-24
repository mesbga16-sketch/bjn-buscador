"""
Extracción de citas legales uruguayas desde texto libre.

La extracción conserva la cita tal como aparece y agrega campos normalizados para
que el verificador pueda construir consultas tolerantes contra IMPO y BJN.
"""

import re

# Admite N°, Nº, N.º, N.o, Nro y Nro.
_NUM_ORD = r'N(?:\s*(?:[°º]|[.]\s*[°º]|o)\.?|ro\.?)\s*'

# Ley 19.355, Ley Nº 18.331, Ley Nro. 18.331/2008 o Ley 18.331 de 2008.
LEY_RE = re.compile(
    rf'\bLey(?:es)?\s*(?:{_NUM_ORD})?'
    rf'(\d{{1,2}}\.?\d{{3}})'
    rf'(?:(?:\s*(?:/|de)\s*(\d{{2,4}}))|(?!\s*(?:/|de)\s*\d))',
    re.IGNORECASE,
)

# Decreto-Ley 14.500, con año opcional cuando aparece en el escrito.
DECRETO_LEY_RE = re.compile(
    rf'\bDecreto[\s-]*Ley(?:es)?\s*(?:{_NUM_ORD})?'
    rf'(\d{{1,2}}\.?\d{{3}})'
    rf'(?:(?:\s*(?:/|de)\s*(\d{{2,4}}))|(?!\s*(?:/|de)\s*\d))',
    re.IGNORECASE,
)

# Decreto 500/991, Decreto Nº 152/013, Decreto 152 de 2013 o Decreto Nº 152.
DECRETO_RE = re.compile(
    rf'\bDecretos?\s*(?:{_NUM_ORD})?'
    rf'(\d{{1,4}})'
    rf'(?:(?:\s*(?:/|de)\s*(\d{{2,4}}))|(?!\s*(?:/|de)\s*\d))',
    re.IGNORECASE,
)

# Sentencia Nº 123/2020, SEF 0003-000100/2014, IUE 2-12345/2021 o Fallo 1.234/2019.
JURISPRUDENCIA_RE = re.compile(
    rf'\b(Sentencias?|SEF|IUE|Fallos?|SCJ|TAT|TAC|TAP|TCA|DFA)\s*[\s-]*'
    rf'(?:{_NUM_ORD})?'
    rf'(\d{{1,4}}(?:[.-]\d{{1,6}})*)\s*/\s*(\d{{2,4}})',
    re.IGNORECASE,
)

# Referencias españolas que aparecen en resoluciones extranjeras (por ejemplo,
# "Roj SAN 2879/2026" y "ECLI:ES:AN:2026:2879"). Se etiquetan para no
# enviarlas al BJN uruguayo durante la verificación automática.
ROJ_RE = re.compile(
    r'\bRoj\s+([A-Z]{2,8})\s+(\d{1,8})\s*/\s*(\d{4})\b',
    re.IGNORECASE,
)
ECLI_RE = re.compile(
    r'\bECLI:([A-Z]{2}):([A-Z0-9]{1,10}):((?:19|20)\d{2}):([A-Z0-9-]+)\b',
    re.IGNORECASE,
)
JURISPRUDENCIA_ES_RE = re.compile(
    r'\b(STS|STSJ|SAN|SAP|ATS|AAN)\s+(\d{1,8})\s*/\s*((?:19|20)\d{2})\b',
    re.IGNORECASE,
)

# Número de ley escrito sin la palabra Ley (por ejemplo, "la 18.331" o "N° 18.331").
# Se activa solamente si el contexto cercano permite clasificarlo como ley o decreto.
NUMERO_LEY_SUELTO_RE = re.compile(rf'(?<![\w./])(\d{{1,2}}\.?\d{{3}})(?![\w/])')
NUMERO_DECRETO_SUELTO_RE = re.compile(
    r'(?<![\w./])(\d{1,4})\s*/\s*(\d{2,4})(?![\w/])'
)


def _normalizar_anio_corto(anio: str, anio_referencia: int) -> str:
    """Convierte años abreviados uruguayos (013, 991, 25) en años de cuatro cifras."""
    if not anio:
        return None
    anio = str(anio)
    if len(anio) == 4:
        return anio
    if len(anio) == 2:
        candidato = int('20' + anio)
        return str(candidato if candidato <= anio_referencia else int('19' + anio))
    if len(anio) == 3:
        candidato_2000 = int('20' + anio[-2:])
        return str(candidato_2000 if candidato_2000 <= anio_referencia else int('19' + anio[-2:]))
    return anio


def _normalizar_numero(numero: str) -> str:
    return re.sub(r'\s+', '', numero.replace('.', ''))


def _prefijo_jurisprudencia(prefijo: str) -> str:
    prefijo = (prefijo or '').strip().upper()
    if prefijo.startswith('SENT'):
        return 'SENTENCIA'
    if prefijo.startswith('FALL'):
        return 'FALLO'
    return prefijo


def _tipo_contextual(texto: str, inicio: int, fin: int):
    """Infiere el tipo de un número aislado usando la etiqueta legal más cercana."""
    izquierda = texto[max(0, inicio - 90):inicio]
    derecha = texto[fin:min(len(texto), fin + 45)]
    ventana = izquierda + derecha
    candidatos = []
    patrones = [
        ('decreto_ley', r'\bdecreto[\s-]*ley(?:es)?\b'),
        ('decreto', r'\bdecretos?\b'),
        ('ley', r'\bley(?:es)?\b'),
    ]
    for tipo, patron in patrones:
        for match in re.finditer(patron, ventana, re.IGNORECASE):
            # Preferir la aparición más cercana al número en ambos sentidos.
            distancia = abs((match.start() + match.end()) / 2 - len(izquierda))
            candidatos.append((distancia, tipo))
    if not candidatos:
        return None
    candidatos.sort(key=lambda item: item[0])
    return candidatos[0][1]


def _solapa_conocida(inicio: int, fin: int, spans: list[tuple[int, int]]) -> bool:
    return any(inicio < conocido_fin and fin > conocido_inicio for conocido_inicio, conocido_fin in spans)


def extraer_citas(texto: str, anio_referencia: int = 2026) -> list[dict]:
    """
    Devuelve las citas encontradas en orden de aparición y sin duplicados.

    Cada cita contiene ``cita``, ``tipo``, ``numero``, ``anio`` e ``inicio``.
    Las referencias sin año conservan ``anio=None`` para que el verificador pueda
    resolverlas por número o informar que el dato no permite una identificación única.
    """
    encontradas = []
    conocidas = []

    decreto_ley_spans = []
    for m in DECRETO_LEY_RE.finditer(texto):
        span = m.span()
        decreto_ley_spans.append(span)
        conocidas.append(span)
        encontradas.append({
            'cita': m.group(0).strip(),
            'tipo': 'decreto_ley',
            'numero': _normalizar_numero(m.group(1)),
            'anio': _normalizar_anio_corto(m.group(2), anio_referencia),
            'inicio': m.start(),
        })

    for m in LEY_RE.finditer(texto):
        if _solapa_conocida(*m.span(), decreto_ley_spans):
            continue
        conocidas.append(m.span())
        encontradas.append({
            'cita': m.group(0).strip(),
            'tipo': 'ley',
            'numero': _normalizar_numero(m.group(1)),
            'anio': _normalizar_anio_corto(m.group(2), anio_referencia),
            'inicio': m.start(),
        })

    for m in DECRETO_RE.finditer(texto):
        conocidas.append(m.span())
        encontradas.append({
            'cita': m.group(0).strip(),
            'tipo': 'decreto',
            'numero': _normalizar_numero(m.group(1)),
            'anio': _normalizar_anio_corto(m.group(2), anio_referencia),
            'inicio': m.start(),
        })

    for m in JURISPRUDENCIA_RE.finditer(texto):
        conocidas.append(m.span())
        encontradas.append({
            'cita': m.group(0).strip(),
            'tipo': 'jurisprudencia',
            'prefijo': _prefijo_jurisprudencia(m.group(1)),
            'numero': _normalizar_numero(m.group(2)),
            'anio': _normalizar_anio_corto(m.group(3), anio_referencia),
            'inicio': m.start(),
        })

    for m in ROJ_RE.finditer(texto):
        if _solapa_conocida(*m.span(), conocidas):
            continue
        conocidas.append(m.span())
        encontradas.append({
            'cita': m.group(0).strip(),
            'tipo': 'jurisprudencia',
            'prefijo': 'ROJ ' + m.group(1).upper(),
            'numero': _normalizar_numero(m.group(2)),
            'anio': m.group(3),
            'jurisprudencia_extranjera': True,
            'jurisdiccion': 'España',
            'inicio': m.start(),
        })

    for m in ECLI_RE.finditer(texto):
        if _solapa_conocida(*m.span(), conocidas):
            continue
        conocidas.append(m.span())
        encontradas.append({
            'cita': m.group(0).strip(),
            'tipo': 'jurisprudencia',
            'prefijo': 'ECLI',
            'numero': m.group(4).upper(),
            'anio': m.group(3),
            'ecli': m.group(0).strip(),
            'jurisprudencia_extranjera': True,
            'jurisdiccion': m.group(1).upper(),
            'inicio': m.start(),
        })

    for m in JURISPRUDENCIA_ES_RE.finditer(texto):
        if _solapa_conocida(*m.span(), conocidas):
            continue
        conocidas.append(m.span())
        encontradas.append({
            'cita': m.group(0).strip(),
            'tipo': 'jurisprudencia',
            'prefijo': m.group(1).upper(),
            'numero': _normalizar_numero(m.group(2)),
            'anio': m.group(3),
            'jurisprudencia_extranjera': True,
            'jurisdiccion': 'España',
            'inicio': m.start(),
        })

    # Referencias sin prefijo, cuando un término Ley/Decreto cercano permite clasificarlas.
    for m in NUMERO_LEY_SUELTO_RE.finditer(texto):
        if _solapa_conocida(*m.span(), conocidas):
            continue
        tipo = _tipo_contextual(texto, m.start(), m.end())
        if tipo not in {'ley', 'decreto_ley'}:
            continue
        encontradas.append({
            'cita': m.group(0).strip(),
            'tipo': tipo,
            'numero': _normalizar_numero(m.group(1)),
            'anio': None,
            'inicio': m.start(),
            'inferido': True,
        })

    # Referencias tipo 152/013 sin la palabra Decreto, si el contexto la identifica.
    for m in NUMERO_DECRETO_SUELTO_RE.finditer(texto):
        if _solapa_conocida(*m.span(), conocidas):
            continue
        tipo = _tipo_contextual(texto, m.start(), m.end())
        if tipo not in {'decreto', 'decreto_ley'}:
            continue
        encontradas.append({
            'cita': m.group(0).strip(),
            'tipo': tipo,
            'numero': _normalizar_numero(m.group(1)),
            'anio': _normalizar_anio_corto(m.group(2), anio_referencia),
            'inicio': m.start(),
            'inferido': True,
        })

    encontradas.sort(key=lambda c: c['inicio'])

    vistos = set()
    resultado = []
    for cita in encontradas:
        clave = (cita['tipo'], cita['numero'], cita.get('anio'), cita.get('prefijo'))
        if clave in vistos:
            continue
        vistos.add(clave)
        resultado.append(cita)
    return resultado
