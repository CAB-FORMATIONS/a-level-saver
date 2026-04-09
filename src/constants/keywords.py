# Keyword lists loaded from config/keywords.yaml — single source of truth
import yaml
from pathlib import Path

_KEYWORDS_FILE = Path(__file__).parent.parent.parent / 'config' / 'keywords.yaml'

with open(_KEYWORDS_FILE, 'r', encoding='utf-8') as _f:
    _kw = yaml.safe_load(_f)

ANNULATION_MARKERS: list = _kw['annulation_markers']
CMA_MARKERS: list = _kw['cma_markers']
ANNULATION_KEYWORDS: list = _kw['annulation_keywords']
SPAM_KEYWORDS: list = _kw['spam_keywords']
BOUNCE_KEYWORDS: list = _kw['bounce_keywords']
CMA_EMAIL_DOMAINS: list = _kw['cma_email_domains']
REPLY_MARKERS: list = _kw['reply_markers']
BATCH_EXCLUSION: list = _kw['batch_exclusion']
SALESIQ_MARKERS: list = _kw['salesiq_markers']
NON_UBER_REGISTRATION: list = _kw['non_uber_registration']
DUPLICATE_MARKERS: list = _kw['duplicate_markers']
UBER_CONVERTED: list = _kw['uber_converted']
INFO_REQUEST: list = _kw['info_request']
OUT_OF_SCOPE: list = _kw['out_of_scope']
UBER_KEYWORDS: list = _kw['uber_keywords']
SKIP_PATTERNS: list = _kw['skip_patterns']
LOGO_SIGNATURE_PATTERNS: list = _kw['logo_signature_patterns']
ACCEPTANCE_KEYWORDS: list = _kw['acceptance_keywords']
DOCUMENT_KEYWORDS: list = _kw['document_keywords']

del _kw, _f
