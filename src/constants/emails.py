# System emails and internal domains — single source of truth

# Emails to ignore when extracting candidate email from ticket
SYSTEM_EMAILS = frozenset({
    'contact@evalbox.com',
    'noreply@evalbox.com',
    'doc@cab-formations.fr',
    'contact@cab-formations.fr',
    'admin@cab-formations.fr',
})

# Internal domains — if sender is from these, it's a forward or internal
INTERNAL_DOMAINS = (
    '@cab-formations.fr',
    '@formalogistics.fr',
)

# Domain substrings for quick internal email detection (covers typos like cabformation vs cab-formations)
INTERNAL_DOMAIN_MARKERS = (
    '@cabformation',
    '@cab-formation',
    '@formalogistics',
)

# Company signature
COMPANY_SIGNATURE = "L'équipe CAB Formations"
