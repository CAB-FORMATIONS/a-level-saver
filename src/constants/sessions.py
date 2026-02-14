# Session constants — single source of truth

SESSION_TYPE_JOUR = "cdj"  # Cours Du Jour prefix
SESSION_TYPE_SOIR = "cds"  # Cours Du Soir prefix

SESSION_HOURS = {
    'jour': '8h30-17h30',
    'soir': '18h-22h',
}

SESSION_TYPE_PREFIX = {
    'jour': SESSION_TYPE_JOUR,
    'soir': SESSION_TYPE_SOIR,
}

SESSION_DISPLAY_NAME = {
    'jour': 'Cours du jour',
    'soir': 'Cours du soir',
}


def is_uber_visio_session(lieu_name: str) -> bool:
    """Check if a session location is an Uber VISIO VTC session.

    Consistent filter across the codebase — requires both 'VISIO' and 'VTC'
    in the location name (e.g. 'VISIO - EXAMEN VTC').
    """
    if not lieu_name:
        return False
    lieu = lieu_name.upper()
    return 'VISIO' in lieu and 'VTC' in lieu
