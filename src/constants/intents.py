# Named intent sets — single source of truth
# Used in template_engine.py and doc_ticket_workflow.py to avoid duplicated inline lists

# Intents that require a full recap (bypass ThreadMemory/V3 suppressions)
FULL_RECAP_INTENTS = frozenset({'QUESTION_GENERALE', 'ENVOIE_IDENTIFIANTS'})

# Intents that require the statut section (cannot suppress)
STATUT_INTENTS = frozenset({'STATUT_DOSSIER', 'QUESTION_PROCESSUS', 'QUESTION_DOCUMENTS'})

# Intents that require the dates section (cannot suppress)
DATES_INTENTS = frozenset({'REPORT_DATE', 'DEMANDE_DATE_PLUS_TOT', 'CONFIRMATION_DATE'})

# Intents where candidate has chosen/confirmed a new exam date
DATE_CONFIRMATION_INTENTS = frozenset({'CONFIRMATION_DATE_EXAMEN', 'REPORT_DATE'})

# Intents requiring date enrichment (month/location-specific)
DATE_RELATED_INTENTS = frozenset({
    'REPORT_DATE', 'DEMANDE_DATES_FUTURES', 'DEMANDE_AUTRES_DATES',
    'DEMANDE_AUTRES_DEPARTEMENTS', 'CONFIRMATION_DATE_EXAMEN',
})

# Intents that need alternative exam dates loaded
NEEDS_NEXT_DATES_INTENTS = frozenset({'REPORT_DATE', 'DEMANDE_REINSCRIPTION', 'DEMANDE_ANNULATION'})

# Session-related intents (explicit session change/confirmation)
SESSION_CHANGE_INTENTS = frozenset({'CONFIRMATION_SESSION', 'DEMANDE_CHANGEMENT_SESSION'})

# Reinscription-related intents (re-enable dates for NON ADMIS)
REINSCRIPTION_INTENTS = frozenset({'DEMANDE_REINSCRIPTION', 'REPORT_DATE'})
