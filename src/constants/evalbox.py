# Evalbox status constants — single source of truth
# These are the Zoho CRM 'Evalbox' field values for exam dossier lifecycle

# Statuts impliquant un paiement CMA effectué
PAID_STATUSES = frozenset({'Dossier Synchronisé', 'VALIDE CMA', 'Convoc CMA reçue', 'Refusé CMA'})
PAID_EXCLUDING_REFUSED = frozenset({'Dossier Synchronisé', 'VALIDE CMA', 'Convoc CMA reçue'})

# Statuts bloquant toute modification de date/session
BLOCKING_MODIFICATION = frozenset({'VALIDE CMA', 'Convoc CMA reçue'})
BLOCKING_RESCHEDULE = frozenset({'VALIDE CMA', 'Convoc CMA reçue', 'Refusé CMA'})

# Statuts considérés comme validés par la CMA
VALIDATED = frozenset({'VALIDE CMA', 'Convoc CMA reçue'})

# Statuts "prêt à payer" (avant paiement CMA)
READY_TO_PAY = frozenset({'Pret a payer', 'Pret a payer par cheque'})

# Statuts vides / non commencés
EMPTY = frozenset({None, '', 'N/A', 'None'})

# Statuts avec problème de documents
DOCUMENTS_PROBLEM = frozenset({'Documents refusés', 'Documents manquants'})

# Dossier constitué (a un compte ExamT3P ou en cours)
DOSSIER_CONSTITUE = PAID_STATUSES | READY_TO_PAY | {'Dossier crée'}

# Statuts impliquant la CMA (avec variantes accent/sans accent)
CMA_INVOLVED = frozenset({
    'Dossier Synchronisé', 'Dossier Synchronise',
    'VALIDE CMA', 'Convoc CMA reçue', 'Convoc CMA recue',
    'Refusé CMA', 'Refuse CMA',
    'Documents refusés', 'Documents refuses', 'Documents manquants',
})

# Resultat lifecycle
RESULTAT_COMPLETED = frozenset({
    'ADMIS', 'NON ADMIS', 'NON ADMISSIBLE',
    'ABSENT TH', 'ABSENT PR', 'Convoc pas recu',
    'NON ADMIS PLUS INTERRESSE', 'NON ADMISSIBLE PLUS INTERRESSE',
})
RESULTAT_MID = frozenset({'ADMISSIBLE'})

# Display mapping (CRM value → user-facing text)
STATUT_DISPLAY = {
    'Dossier crée': 'Dossier en cours de création',
    'Pret a payer': 'Dossier prêt pour paiement CMA',
    'Pret a payer par cheque': 'Dossier prêt pour paiement CMA',
    'Dossier Synchronisé': 'Dossier transmis à la CMA (instruction en cours)',
    'Dossier Synchronise': 'Dossier transmis à la CMA (instruction en cours)',
    'VALIDE CMA': 'Dossier validé par la CMA',
    'Convoc CMA reçue': 'Convocation disponible',
    'Convoc CMA recue': 'Convocation disponible',
    'Refusé CMA': 'Document(s) refusé(s) par la CMA',
    'Refuse CMA': 'Document(s) refusé(s) par la CMA',
    'Documents refusés': 'Document(s) refusé(s)',
    'Documents refuses': 'Document(s) refusé(s)',
    'Documents manquants': 'Document(s) manquant(s)',
}
