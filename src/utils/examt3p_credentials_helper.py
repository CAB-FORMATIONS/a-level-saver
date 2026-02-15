"""
Helper pour gérer les identifiants ExamT3P et leur validation.

Workflow complet :
1. Recherche identifiants dans Zoho CRM
2. Si absents, recherche dans les threads de mail
3. Si aucun identifiant trouvé : Ne PAS demander au candidat (création de compte par nous)
4. Test de connexion OBLIGATOIRE pour les identifiants trouvés
5. Mise à jour Zoho si identifiants trouvés dans les mails et connexion OK
6. Si connexion échoue : Demander au candidat de réinitialiser via "Mot de passe oublié ?"
"""
import logging
import re
from typing import Dict, Optional, Tuple, List
from pathlib import Path

# Load environment variables for Anthropic API key
from dotenv import load_dotenv
project_root = Path(__file__).parent.parent.parent
load_dotenv(project_root / ".env")

from src.constants.models import MODEL_EXTRACTION
from src.constants.urls import EXAMT3P_LOGIN_URL

logger = logging.getLogger(__name__)


def extract_credentials_from_threads(threads: List[Dict]) -> Optional[Dict[str, str]]:
    """
    Extrait les identifiants ExamT3P depuis les threads de mail en utilisant l'IA.

    Utilise Claude pour comprendre le contexte et extraire:
    - L'identifiant (généralement un email)
    - Le mot de passe

    Args:
        threads: Liste des threads de ticket (direction 'in' = messages client)

    Returns:
        Dict avec 'identifiant' et 'mot_de_passe' si trouvés, None sinon
    """
    from src.utils.text_utils import get_clean_thread_content

    # Collecter le contenu des messages entrants (du candidat)
    messages_content = []
    for thread in threads:
        if thread.get('direction') != 'in':
            continue
        content = get_clean_thread_content(thread)
        if content and len(content.strip()) > 10:
            messages_content.append(content)

    if not messages_content:
        logger.info("Pas de messages entrants dans les threads")
        return None

    # Concaténer les messages (limiter la taille)
    all_content = "\n---\n".join(messages_content[:5])  # Max 5 messages
    if len(all_content) > 3000:
        all_content = all_content[:3000]

    # Appeler Claude pour extraire les identifiants
    try:
        from anthropic import Anthropic

        client = Anthropic()

        prompt = f"""Analyse ces messages d'un candidat et extrait ses identifiants de connexion ExamT3P s'il les a communiqués.

MESSAGES DU CANDIDAT:
{all_content}

INSTRUCTIONS:
1. Cherche un email qui pourrait être son identifiant de connexion (pas les emails @cab-formations.fr)
2. Cherche un mot de passe qu'il aurait communiqué (souvent après "mot de passe:", "mdp:", ou sur une ligne séparée)
3. Le mot de passe peut être écrit sans label, juste comme une chaîne alphanumérique

Réponds UNIQUEMENT en JSON valide (sans texte avant/après):
{{
    "identifiant": "email@exemple.com ou null si non trouvé",
    "mot_de_passe": "le_mot_de_passe ou null si non trouvé",
    "confidence": 0.0-1.0
}}

Si tu ne trouves pas d'identifiants, réponds:
{{"identifiant": null, "mot_de_passe": null, "confidence": 0}}"""

        response = client.messages.create(
            model=MODEL_EXTRACTION,  # Modèle rapide pour extraction
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}]
        )

        response_text = response.content[0].text.strip()
        logger.debug(f"Réponse IA extraction credentials: {response_text}")

        # Parser le JSON
        import json

        # Nettoyer le JSON si nécessaire
        if response_text.startswith("```"):
            response_text = response_text.split("```")[1]
            if response_text.startswith("json"):
                response_text = response_text[4:]

        # Extraire uniquement le JSON
        start_idx = response_text.find('{')
        end_idx = response_text.rfind('}') + 1
        if start_idx != -1 and end_idx > start_idx:
            response_text = response_text[start_idx:end_idx]

        result = json.loads(response_text)

        identifiant = result.get('identifiant')
        mot_de_passe = result.get('mot_de_passe')
        confidence = result.get('confidence', 0)

        # Valider les résultats
        if identifiant and identifiant.lower() != 'null' and mot_de_passe and mot_de_passe.lower() != 'null':
            logger.info(f"✅ Identifiants extraits par IA (confidence: {confidence}): {identifiant} / ****")
            return {
                'identifiant': identifiant,
                'mot_de_passe': mot_de_passe,
                'source': 'email_threads',
                'extraction_method': 'ai',
                'confidence': confidence
            }

        # Résultat partiel
        if identifiant and identifiant.lower() != 'null':
            logger.warning(f"Identifiant trouvé mais pas de mot de passe: {identifiant}")
        if mot_de_passe and mot_de_passe.lower() != 'null':
            logger.warning("Mot de passe trouvé mais pas d'identifiant")

        if (identifiant and identifiant.lower() != 'null') or (mot_de_passe and mot_de_passe.lower() != 'null'):
            logger.warning(
                f"Identifiants incomplets trouvés dans les threads: "
                f"identifiant={'Oui' if identifiant and identifiant.lower() != 'null' else 'Non'}, "
                f"mot_de_passe={'Oui' if mot_de_passe and mot_de_passe.lower() != 'null' else 'Non'}"
            )

        return None

    except Exception as e:
        logger.error(f"Erreur extraction IA des identifiants: {e}")
        return None


def test_examt3p_connection(identifiant: str, mot_de_passe: str) -> Tuple[bool, Optional[str]]:
    """
    Test la connexion ExamT3P avec les identifiants fournis.

    Args:
        identifiant: IDENTIFIANT_EVALBOX
        mot_de_passe: MDP_EVALBOX

    Returns:
        Tuple (success: bool, error_message: str or None)
    """
    import asyncio

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        logger.error("Module playwright non installé")
        return False, "Module playwright non installé - impossible de tester la connexion"

    logger.info(f"Test de connexion ExamT3P pour {identifiant}...")

    async def test_login():
        """Test de login asynchrone."""
        try:
            async with async_playwright() as p:
                # Lancer le navigateur en mode headless
                # Note: Playwright trouvera automatiquement le navigateur installé (cross-platform)
                browser = await p.chromium.launch(
                    headless=True,
                    args=['--no-sandbox', '--disable-setuid-sandbox', '--disable-dev-shm-usage']
                )

                context = await browser.new_context(viewport={'width': 1280, 'height': 720})
                context.set_default_timeout(30000)  # 30 secondes
                page = await context.new_page()

                try:
                    # Accéder à la page de connexion
                    await page.goto(EXAMT3P_LOGIN_URL, wait_until='networkidle', timeout=30000)
                    await asyncio.sleep(3)  # Augmenté pour s'assurer que la page est chargée

                    # Cliquer sur "Me connecter" pour ouvrir la modal
                    try:
                        me_connecter_btn = await page.wait_for_selector('button:has-text("Me connecter")', timeout=10000)
                        if me_connecter_btn:
                            await me_connecter_btn.click()
                            await asyncio.sleep(1)
                    except Exception as e:
                        pass

                    # Remplir le formulaire
                    email_selectors = ['#loginEmail', 'input[type="email"]', 'input[name="email"]']
                    email_filled = False
                    for selector in email_selectors:
                        try:
                            await page.wait_for_selector(selector, state='visible', timeout=5000)
                            await page.fill(selector, identifiant)
                            email_filled = True
                            break
                        except Exception as e:
                            continue

                    if not email_filled:
                        return False, "Champ email non trouvé"

                    password_selectors = ['#loginPassword', 'input[type="password"]', 'input[name="password"]']
                    password_filled = False
                    for selector in password_selectors:
                        try:
                            await page.fill(selector, mot_de_passe)
                            password_filled = True
                            break
                        except Exception as e:
                            continue

                    if not password_filled:
                        return False, "Champ mot de passe non trouvé"

                    # Cliquer sur le bouton de connexion
                    submit_selectors = [
                        '#loginModal button:has-text("Se connecter")',
                        'button:has-text("Se connecter")',
                        'button[type="submit"]'
                    ]

                    submitted = False
                    for selector in submit_selectors:
                        try:
                            btn = await page.query_selector(selector)
                            if btn:
                                await btn.click()
                                submitted = True
                                break
                        except Exception as e:
                            continue

                    if not submitted:
                        await page.keyboard.press('Enter')

                    # Attendre la navigation (augmenté pour laisser la page charger)
                    await asyncio.sleep(5)

                    # Vérifier si connecté - mêmes indicateurs que exament3p_playwright.py
                    success_indicators = [
                        "Vue d'ensemble",
                        "Mon Espace Candidat",
                        "Déconnexion",
                        "Bienvenue",
                        "monEspaceContainer"  # ID/class présent sur la page après login
                    ]

                    content = await page.content()
                    for indicator in success_indicators:
                        if indicator in content:
                            return True, None

                    # Vérifier l'URL
                    current_url = page.url
                    if "mon-espace" in current_url or "dashboard" in current_url or "espace-candidat" in current_url:
                        return True, None

                    # Vérifier si erreur de connexion visible
                    error_indicators = [
                        "Identifiant ou mot de passe incorrect",
                        "invalid",
                        "erreur",
                        "échec",
                        "Mot de passe oublié"  # Si on voit encore ce bouton, on n'est pas connecté
                    ]
                    content_lower = content.lower()
                    for error in error_indicators:
                        if error.lower() in content_lower and "Me connecter" in content:
                            return False, "Identifiants invalides"

                    # Si on ne trouve pas les indicateurs mais qu'on n'est plus sur la page de login
                    if "Me connecter" not in content:
                        # Probablement connecté mais page différente
                        return True, None

                    return False, "Connexion échouée - page d'accueil non détectée"

                finally:
                    await browser.close()

        except Exception as e:
            return False, f"Erreur lors du test de connexion: {str(e)}"

    try:
        # Exécuter le test de login
        success, error = asyncio.run(test_login())

        if success:
            logger.info("✅ Test de connexion ExamT3P réussi")
            return True, None
        else:
            logger.warning(f"❌ Test de connexion ExamT3P échoué: {error}")
            return False, error

    except Exception as e:
        logger.error(f"❌ Erreur lors du test de connexion ExamT3P: {e}")
        return False, str(e)


def _is_account_paid(examt3p_data: Dict, account_label: str = "compte") -> bool:
    """
    Détermine si un compte ExamT3P a déjà été payé.

    Un compte est considéré comme payé si:
    - statut_dossier est "Valide", "En attente de convocation", "En cours d'instruction"
    - OU paiement_cma.statut == "VALIDÉ"
    - OU historique_paiements contient au moins un paiement VALIDÉ

    Args:
        examt3p_data: Données extraites du compte ExamT3P
        account_label: Label pour les logs (ex: "CRM", "Thread")

    Returns:
        True si le compte est payé, False sinon
    """
    logger.info(f"     🔍 Analyse paiement {account_label}:")

    if not examt3p_data or examt3p_data.get('error'):
        logger.info(f"        ❌ Données vides ou erreur")
        return False

    # Vérifier le statut du dossier
    statut = examt3p_data.get('statut_dossier', '').lower()
    logger.info(f"        📋 statut_dossier: '{examt3p_data.get('statut_dossier', 'N/A')}'")

    statuts_payes = [
        'valide',
        'en attente de convocation',
        'en cours d\'instruction',
        'en attente d\'instruction des pièces',
        'dossier validé'
    ]
    if any(s in statut for s in statuts_payes):
        matched = [s for s in statuts_payes if s in statut][0]
        logger.info(f"        ✅ PAYÉ via statut_dossier (match: '{matched}')")
        return True

    # Vérifier le paiement CMA
    paiement_cma = examt3p_data.get('paiement_cma', {})
    logger.info(f"        💳 paiement_cma: {paiement_cma}")
    if paiement_cma.get('statut', '').upper() == 'VALIDÉ':
        logger.info(f"        ✅ PAYÉ via paiement_cma.statut = 'VALIDÉ'")
        return True

    # Vérifier l'historique des paiements
    historique = examt3p_data.get('historique_paiements', [])
    logger.info(f"        📜 historique_paiements: {len(historique)} entrée(s)")
    for i, paiement in enumerate(historique):
        logger.info(f"           [{i}] {paiement}")
        if paiement.get('statut', '').upper() == 'VALIDÉ':
            logger.info(f"        ✅ PAYÉ via historique_paiements[{i}].statut = 'VALIDÉ'")
            return True

    # Vérifier la progression
    progression = examt3p_data.get('progression', {})
    logger.info(f"        📊 progression: {progression}")
    if progression.get('paiement', '').upper() == 'VALIDÉ':
        logger.info(f"        ✅ PAYÉ via progression.paiement = 'VALIDÉ'")
        return True

    logger.info(f"        ❌ NON PAYÉ (aucun critère rempli)")
    return False


def get_credentials_with_validation(
    deal_data: Dict,
    threads: List[Dict],
    crm_client=None,
    deal_id: Optional[str] = None,
    auto_update_crm: bool = False
) -> Dict:
    """
    Workflow complet pour récupérer et valider les identifiants ExamT3P.

    Étapes:
    1. Chercher dans le CRM (deal_data)
    2. TOUJOURS chercher aussi dans les threads de mail
    3. Tester la connexion CRM d'abord
    4. Si CRM échoue ET threads ont des identifiants différents → tester ceux-là
    5. Utiliser ceux qui marchent, MAJ CRM si nécessaire

    Args:
        deal_data: Données du deal CRM
        threads: Threads du ticket
        crm_client: Client Zoho CRM (pour mise à jour)
        deal_id: ID du deal (pour mise à jour)
        auto_update_crm: Mettre à jour automatiquement le CRM

    Returns:
        {
            'credentials_found': bool,
            'credentials_source': 'crm' | 'email_threads' | None,
            'identifiant': str or None,
            'mot_de_passe': str or None,
            'connection_test_success': bool,
            'connection_error': str or None,
            'crm_updated': bool,
            'should_respond_to_candidate': bool,
            'candidate_response_message': str or None
        }
    """
    result = {
        'credentials_found': False,
        'credentials_source': None,
        'identifiant': None,
        'mot_de_passe': None,
        'connection_test_success': False,
        'connection_error': None,
        'crm_updated': False,
        'should_respond_to_candidate': False,
        'candidate_response_message': None
    }

    # ================================================================
    # ÉTAPE 1: Chercher dans le CRM
    # ================================================================
    logger.info("🔍 Recherche des identifiants ExamT3P...")
    logger.info("  Étape 1/4: Vérification dans le CRM...")

    identifiant_crm = deal_data.get('IDENTIFIANT_EVALBOX')
    mdp_crm = deal_data.get('MDP_EVALBOX')

    if identifiant_crm and mdp_crm:
        logger.info(f"  ✅ Identifiants trouvés dans le CRM: {identifiant_crm}")
    else:
        logger.info("  ⚠️  Identifiants absents du CRM")

    # ================================================================
    # ÉTAPE 2: TOUJOURS chercher dans les threads de mail
    # (même si CRM a des identifiants - le candidat peut avoir envoyé de nouveaux)
    # ================================================================
    logger.info("  Étape 2/4: Recherche dans les threads de mail...")

    credentials_from_threads = extract_credentials_from_threads(threads)
    identifiant_threads = None
    mdp_threads = None

    if credentials_from_threads:
        identifiant_threads = credentials_from_threads['identifiant']
        mdp_threads = credentials_from_threads['mot_de_passe']
        logger.info(f"  ✅ Identifiants trouvés dans les threads: {identifiant_threads}")

        # Comparer avec CRM
        if identifiant_crm and identifiant_threads:
            if identifiant_threads.lower() != identifiant_crm.lower():
                logger.info(f"  ⚠️  Identifiants DIFFÉRENTS: CRM={identifiant_crm} vs Threads={identifiant_threads}")
    else:
        logger.info("  ⚠️  Pas d'identifiants dans les threads")

    # ================================================================
    # ÉTAPE 3: Déterminer quels identifiants tester
    # Priorité: CRM d'abord, puis threads si différents
    # ================================================================
    identifiant = None
    mot_de_passe = None
    source = None

    # Cas 1: CRM a des identifiants → les tester d'abord
    if identifiant_crm and mdp_crm:
        identifiant = identifiant_crm
        mot_de_passe = mdp_crm
        source = 'crm'
        result['credentials_found'] = True
        result['credentials_source'] = 'crm'
    # Cas 2: Pas de CRM mais threads ont des identifiants
    elif identifiant_threads and mdp_threads:
        identifiant = identifiant_threads
        mot_de_passe = mdp_threads
        source = 'email_threads'
        result['credentials_found'] = True
        result['credentials_source'] = 'email_threads'

    # Si aucun identifiant trouvé nulle part (ni CRM ni threads)...
    if not identifiant or not mot_de_passe:
        # ================================================================
        # VÉRIFICATION CRITIQUE: Avons-nous déjà demandé au candidat
        # ses identifiants OU de créer son compte?
        # Si oui → on doit lui redemander
        # ================================================================

        # CAS 1: On a demandé les identifiants (compte déjà créé)
        if detect_credentials_request_in_history(threads):
            logger.warning("⚠️  Identifiants non trouvés MAIS demande d'identifiants déjà faite!")
            logger.info("→ On doit redemander les identifiants au candidat")

            # Détecter si le candidat a exprimé une préférence de cours
            session_preference = detect_session_preference_in_threads(threads)
            if session_preference:
                logger.info(f"  📚 Préférence de cours détectée: {session_preference}")

            result['should_respond_to_candidate'] = True
            result['candidate_response_message'] = generate_credentials_request_followup_response(
                include_session_preference=session_preference
            )
            result['credentials_request_sent'] = True  # Flag pour traçabilité
            result['session_preference'] = session_preference  # Pour traçabilité
            return result

        # CAS 2: On a demandé de créer le compte
        if detect_account_creation_request_in_history(threads):
            logger.warning("⚠️  Identifiants non trouvés MAIS création de compte déjà demandée!")
            logger.info("→ On doit redemander au candidat s'il a créé son compte")
            result['should_respond_to_candidate'] = True
            result['candidate_response_message'] = generate_account_creation_followup_response()
            result['account_creation_requested'] = True  # Flag pour traçabilité
            return result

        # Sinon, c'est nous qui créerons le compte (Uber 20€ par exemple)
        logger.warning("❌ Identifiants ExamT3P non trouvés - Création de compte par nous")
        result['should_respond_to_candidate'] = False  # Pas de demande au candidat
        result['candidate_response_message'] = None
        return result

    result['identifiant'] = identifiant
    result['mot_de_passe'] = mot_de_passe

    # ================================================================
    # ÉTAPE 3: TEST DE CONNEXION (OBLIGATOIRE)
    # ================================================================
    logger.info(f"  Étape 3/4: Test de connexion ({source})...")

    connection_ok, connection_error = test_examt3p_connection(identifiant, mot_de_passe)

    # ================================================================
    # ÉTAPE 4: Gestion intelligente des identifiants multiples
    # ================================================================
    # Vérifier si les threads ont des identifiants DIFFÉRENTS
    threads_have_different_creds = False
    if identifiant_threads and mdp_threads and identifiant_crm and mdp_crm:
        threads_have_different_creds = (
            identifiant_threads.lower() != identifiant_crm.lower() or
            mdp_threads != mdp_crm
        )

    # CAS 1: CRM échoue → tester threads
    if not connection_ok and source == 'crm' and identifiant_threads and mdp_threads:
        if threads_have_different_creds:
            logger.info(f"  🔄 CRM échoué, test des identifiants des threads: {identifiant_threads}")
            connection_ok_threads, connection_error_threads = test_examt3p_connection(
                identifiant_threads, mdp_threads
            )

            if connection_ok_threads:
                logger.info("  ✅ Identifiants des threads VALIDES!")
                identifiant = identifiant_threads
                mot_de_passe = mdp_threads
                source = 'email_threads'
                connection_ok = True
                connection_error = None
                result['identifiant'] = identifiant
                result['mot_de_passe'] = mot_de_passe
                result['credentials_source'] = 'email_threads'

                # Mettre à jour le CRM
                if auto_update_crm and crm_client and deal_id:
                    logger.info("  📝 Mise à jour du CRM avec les identifiants corrigés...")
                    try:
                        crm_client.update_deal(deal_id, {
                            'IDENTIFIANT_EVALBOX': identifiant,
                            'MDP_EVALBOX': mot_de_passe
                        })
                        logger.info("  ✅ CRM mis à jour avec les nouveaux identifiants")
                        result['crm_updated'] = True
                    except Exception as e:
                        logger.error(f"  ❌ Erreur mise à jour CRM: {e}")
            else:
                logger.warning(f"  ❌ Identifiants threads également invalides: {connection_error_threads}")
        else:
            logger.info("  ⚠️  Threads ont les mêmes identifiants que CRM - pas de retry")

    # CAS 2: CRM fonctionne MAIS threads ont des identifiants DIFFÉRENTS
    # → Il faut vérifier si on doit basculer sur le compte du candidat
    elif connection_ok and source == 'crm' and threads_have_different_creds:
        logger.info(f"  🔍 CRM OK mais threads ont des identifiants différents: {identifiant_threads}")
        logger.info("  🔍 Test du compte candidat pour comparaison...")

        connection_ok_threads, _ = test_examt3p_connection(identifiant_threads, mdp_threads)

        if connection_ok_threads:
            # LES DEUX COMPTES FONCTIONNENT → Décision basée sur le statut de paiement
            logger.info("  ⚠️  DEUX COMPTES VALIDES DÉTECTÉS!")
            logger.info("  🔍 Extraction des données pour comparaison...")

            # Importer l'extracteur ExamT3P
            try:
                from exament3p_playwright import extract_exament3p_sync

                # Extraire les données des deux comptes
                logger.info(f"  📊 Extraction compte CRM: {identifiant_crm}")
                data_crm = extract_exament3p_sync(identifiant_crm, mdp_crm, max_retries=1)

                logger.info(f"  📊 Extraction compte Thread: {identifiant_threads}")
                data_threads = extract_exament3p_sync(identifiant_threads, mdp_threads, max_retries=1)

                # Analyser les statuts de paiement
                crm_paid = _is_account_paid(data_crm, "CRM")
                threads_paid = _is_account_paid(data_threads, "Thread")

                logger.info(f"  💰 Résultat - Compte CRM payé: {crm_paid}")
                logger.info(f"  💰 Résultat - Compte Thread payé: {threads_paid}")

                # RÈGLES DE DÉCISION
                if crm_paid and threads_paid:
                    # ⚠️ DOUBLON DE PAIEMENT - Alerter!
                    logger.error("  🚨 ALERTE: DEUX COMPTES PAYÉS DÉTECTÉS!")
                    logger.error(f"     CRM: {identifiant_crm}")
                    logger.error(f"     Thread: {identifiant_threads}")
                    result['duplicate_payment_alert'] = True
                    result['duplicate_accounts'] = {
                        'crm': {'identifiant': identifiant_crm, 'paid': True},
                        'thread': {'identifiant': identifiant_threads, 'paid': True}
                    }
                    # Garder le compte CRM (déjà en place), mais alerter
                    logger.warning("  → Garde du compte CRM, intervention manuelle requise")

                elif crm_paid and not threads_paid:
                    # CRM payé, threads non → Garder CRM + AVERTISSEMENT au candidat
                    logger.info("  ✅ Compte CRM déjà payé → On le garde")
                    logger.warning(f"  ⚠️  ATTENTION: Compte personnel détecté ({identifiant_threads}) - non payé")
                    logger.info("  → Avertissement à inclure dans la réponse au candidat")

                    # Flag pour déclencher l'état A4 et le warning dans la réponse
                    result['personal_account_warning'] = True
                    result['personal_account_email'] = identifiant_threads
                    result['cab_account_email'] = identifiant_crm
                    # Ajouter la date de paiement pour l'inclure dans le message
                    paiement_cma = data_crm.get('paiement_cma', {})
                    result['cab_payment_date'] = paiement_cma.get('date', '')
                    # Pas de changement d'identifiants, on garde le compte CRM

                elif not crm_paid and threads_paid:
                    # Thread payé, CRM non → Basculer sur thread!
                    logger.info("  🔄 Compte candidat déjà payé → Bascule sur ses identifiants")
                    identifiant = identifiant_threads
                    mot_de_passe = mdp_threads
                    source = 'email_threads'
                    result['identifiant'] = identifiant
                    result['mot_de_passe'] = mot_de_passe
                    result['credentials_source'] = 'email_threads'
                    result['switched_to_paid_account'] = True

                    # Mettre à jour le CRM
                    if auto_update_crm and crm_client and deal_id:
                        logger.info("  📝 Mise à jour du CRM avec le compte payé du candidat...")
                        try:
                            crm_client.update_deal(deal_id, {
                                'IDENTIFIANT_EVALBOX': identifiant,
                                'MDP_EVALBOX': mot_de_passe
                            })
                            logger.info("  ✅ CRM mis à jour")
                            result['crm_updated'] = True
                        except Exception as e:
                            logger.error(f"  ❌ Erreur mise à jour CRM: {e}")

                else:
                    # Aucun n'est payé → Préférer le compte du candidat
                    logger.info("  🔄 Aucun compte payé → Préférence au compte candidat")
                    identifiant = identifiant_threads
                    mot_de_passe = mdp_threads
                    source = 'email_threads'
                    result['identifiant'] = identifiant
                    result['mot_de_passe'] = mot_de_passe
                    result['credentials_source'] = 'email_threads'

                    # Mettre à jour le CRM
                    if auto_update_crm and crm_client and deal_id:
                        logger.info("  📝 Mise à jour du CRM avec les identifiants du candidat...")
                        try:
                            crm_client.update_deal(deal_id, {
                                'IDENTIFIANT_EVALBOX': identifiant,
                                'MDP_EVALBOX': mot_de_passe
                            })
                            logger.info("  ✅ CRM mis à jour")
                            result['crm_updated'] = True
                        except Exception as e:
                            logger.error(f"  ❌ Erreur mise à jour CRM: {e}")

            except ImportError:
                logger.warning("  ⚠️  Module exament3p_playwright non disponible pour comparaison")
                logger.info("  → On garde le compte CRM par défaut")
            except Exception as e:
                logger.error(f"  ❌ Erreur lors de la comparaison des comptes: {e}")
                logger.info("  → On garde le compte CRM par défaut")
        else:
            # Le compte thread ne fonctionne pas avec le mot de passe extrait
            # MAIS le candidat pourrait avoir un compte perso avec un AUTRE mot de passe !
            logger.info("  ✅ Compte thread invalide, on garde le compte CRM")
            logger.warning(f"  ⚠️  ATTENTION: Le candidat a peut-être un compte personnel avec {identifiant_threads}")
            logger.warning("     Le test a échoué car le mot de passe extrait est probablement différent")
            result['potential_personal_account'] = True
            result['potential_personal_email'] = identifiant_threads
            result['personal_account_warning'] = (
                f"Le candidat a potentiellement un compte ExamT3P personnel avec l'email {identifiant_threads}. "
                "Il pourrait se connecter à ce compte et voir un statut différent (non payé, non validé). "
                "La réponse doit clairement indiquer d'utiliser UNIQUEMENT le compte CAB."
            )

    result['connection_test_success'] = connection_ok
    result['connection_error'] = connection_error

    # ================================================================
    # ÉTAPE 5: Actions selon résultat du test
    # ================================================================
    if connection_ok:
        logger.info("✅ Connexion ExamT3P validée")

        # Si identifiants viennent des mails et pas encore mis à jour, mettre à jour le CRM
        if source == 'email_threads' and not result.get('crm_updated') and auto_update_crm and crm_client and deal_id:
            logger.info("📝 Mise à jour du CRM avec les identifiants trouvés dans les mails...")
            try:
                crm_client.update_deal(deal_id, {
                    'IDENTIFIANT_EVALBOX': identifiant,
                    'MDP_EVALBOX': mot_de_passe
                })
                logger.info("✅ CRM mis à jour avec les nouveaux identifiants")
                result['crm_updated'] = True
            except Exception as e:
                logger.error(f"❌ Erreur lors de la mise à jour du CRM: {e}")

    else:
        # ================================================================
        # ÉTAPE 6: Connexion échouée → Réponse au candidat
        # ================================================================
        logger.warning(f"❌ Connexion ExamT3P échouée: {connection_error}")
        result['should_respond_to_candidate'] = True

        # Générer le message selon la source des identifiants
        if source == 'crm':
            result['candidate_response_message'] = generate_invalid_credentials_response_crm()
        else:
            result['candidate_response_message'] = generate_invalid_credentials_response_email()

    return result


def generate_invalid_credentials_response_crm() -> str:
    """
    Génère le message à envoyer au candidat quand les identifiants du CRM ne fonctionnent pas.
    """
    return """Bonjour,

Nous avons tenté d'accéder à votre dossier sur la plateforme ExamenT3P avec les identifiants que vous nous aviez précédemment transmis, mais la connexion a échoué.

Il est possible que vous ayez modifié votre mot de passe depuis.

Pour accéder à votre compte, veuillez suivre la procédure de réinitialisation :

1. Rendez-vous sur la plateforme ExamenT3P : https://www.exament3p.fr
2. Cliquez sur "Me connecter"
3. Utilisez la fonction "Mot de passe oublié ?"
4. Suivez les instructions pour réinitialiser votre mot de passe

Une fois votre mot de passe réinitialisé, merci de nous transmettre vos nouveaux identifiants afin que nous puissions assurer le suivi de votre dossier.

Cordialement,
L'équipe DOC"""


def generate_invalid_credentials_response_email() -> str:
    """
    Génère le message à envoyer au candidat quand les identifiants trouvés dans les mails ne fonctionnent pas.
    """
    return """Bonjour,

Nous avons tenté d'accéder à votre dossier sur la plateforme ExamenT3P avec les identifiants que vous nous avez transmis, mais la connexion a échoué.

Il est possible que vous ayez modifié votre mot de passe ou que les identifiants ne soient plus à jour.

Pour accéder à votre compte, veuillez suivre la procédure de réinitialisation :

1. Rendez-vous sur la plateforme ExamenT3P : https://www.exament3p.fr
2. Cliquez sur "Me connecter"
3. Utilisez la fonction "Mot de passe oublié ?"
4. Suivez les instructions pour réinitialiser votre mot de passe

Une fois votre mot de passe réinitialisé, merci de nous transmettre vos nouveaux identifiants afin que nous puissions assurer le suivi de votre dossier.

Cordialement,
L'équipe DOC"""


def detect_account_creation_request_in_history(threads: List[Dict]) -> bool:
    """
    Détecte si nous (Cab Formations) avons déjà demandé au candidat de créer
    son compte ExamT3P dans l'historique des échanges.

    Patterns recherchés dans les messages SORTANTS (direction='out'):
    - "créer votre compte"
    - "créez votre compte"
    - "ouvrir un compte"
    - "création de votre compte"
    - "inscription sur ExamT3P"
    - "s'inscrire sur ExamT3P"
    - "vous inscrire sur exament3p"

    Returns:
        True si on a demandé au candidat de créer son compte, False sinon
    """
    from src.utils.text_utils import get_clean_thread_content

    patterns = [
        r'cr[ée]er?\s+votre\s+compte',
        r'cr[ée]ez?\s+votre\s+compte',
        r'ouvrir\s+un\s+compte',
        r"création\s+de\s+votre\s+compte",
        r'inscription\s+sur\s+examen?t3p',
        r"s'inscrire\s+sur\s+examen?t3p",
        r'vous\s+inscrire\s+sur\s+examen?t3p',
        r'cr[ée]er?\s+un\s+compte\s+examen?t3p',
        r'cr[ée]er?\s+un\s+compte\s+sur\s+examen?t3p',
        r'ouvrir\s+votre\s+compte\s+examen?t3p',
        r'inscription\s+à\s+examen?t3p',
        r'vous\s+devez\s+.*cr[ée]er.*compte',
    ]

    for thread in threads:
        # Uniquement les messages SORTANTS (de nous vers le candidat)
        if thread.get('direction') != 'out':
            continue

        content = get_clean_thread_content(thread)
        content_lower = content.lower()

        for pattern in patterns:
            if re.search(pattern, content_lower, re.IGNORECASE):
                logger.info(f"🔍 Détecté: demande de création de compte dans l'historique")
                logger.info(f"   Pattern trouvé: {pattern}")
                return True

    return False


def detect_session_preference_in_threads(threads: List[Dict]) -> Optional[str]:
    """
    Détecte si le candidat a exprimé une préférence pour les cours du jour ou du soir
    dans ses messages.

    Returns:
        "cours du soir" ou "cours du jour" si détecté, None sinon
    """
    from src.utils.text_utils import get_clean_thread_content

    for thread in threads:
        # Uniquement les messages ENTRANTS (du candidat)
        if thread.get('direction') != 'in':
            continue

        content = get_clean_thread_content(thread)
        content_lower = content.lower()

        # Patterns pour cours du soir
        soir_patterns = [
            r'cours\s+du\s+soir',
            r'soir',
            r'18h',
            r'apr[èe]s\s+le\s+travail',
            r'le\s+soir',
            r'en\s+soir[ée]e',
        ]

        # Patterns pour cours du jour
        jour_patterns = [
            r'cours\s+du\s+jour',
            r'journ[ée]e',
            r'matin',
            r'apr[èe]s.midi',
            r'en\s+journ[ée]e',
        ]

        # Vérifier cours du soir en premier (plus commun)
        for pattern in soir_patterns:
            if re.search(pattern, content_lower, re.IGNORECASE):
                logger.info(f"🔍 Préférence détectée: cours du soir (pattern: {pattern})")
                return "cours du soir"

        # Vérifier cours du jour
        for pattern in jour_patterns:
            if re.search(pattern, content_lower, re.IGNORECASE):
                logger.info(f"🔍 Préférence détectée: cours du jour (pattern: {pattern})")
                return "cours du jour"

    return None


def detect_credentials_request_in_history(threads: List[Dict]) -> bool:
    """
    Détecte si nous (Cab Formations) avons déjà demandé au candidat ses
    identifiants ExamT3P dans l'historique des échanges.

    Vérifie DEUX sources:
    1. Nos messages SORTANTS (direction='out') avec patterns de demande
    2. Les messages ENTRANTS (direction='in') où le candidat MENTIONNE qu'on lui a demandé

    Returns:
        True si on a demandé les identifiants au candidat, False sinon
    """
    from src.utils.text_utils import get_clean_thread_content

    # Patterns dans les messages SORTANTS (de nous vers le candidat)
    outgoing_patterns = [
        r'transmettre\s+vos\s+identifiants',
        r'envoyer\s+vos\s+identifiants',
        r'communiquer\s+vos\s+identifiants',
        r'fournir\s+vos\s+identifiants',
        r'vos\s+identifiants\s+examen?t3p',
        r'identifiants\s+de\s+connexion',
        r'email\s+et\s+mot\s+de\s+passe',
        r'identifiant\s+et\s+mot\s+de\s+passe',
        r'nous\s+transmettre.*identifiants',
        r'besoin\s+de\s+vos\s+identifiants',
        r'merci\s+de\s+nous\s+transmettre.*identifiants',
        r'demandons\s+vos\s+identifiants',
    ]

    # Patterns dans les messages ENTRANTS (le candidat mentionne qu'on lui a demandé)
    incoming_patterns = [
        r're[çc]u\s+un\s+mail.*demande.*identifiants',
        r'demande\s+mes\s+identifiants',
        r'me\s+demande\s+mes\s+identifiants',
        r'demand[ée]\s+mes\s+identifiants',
        r'vous\s+m.*avez\s+demand[ée].*identifiants',
        r'on\s+m.*a\s+demand[ée].*identifiants',
        r'mail.*identifiants',
        r'support.*demande.*identifiants',
        r'est.ce\s+.*normal.*identifiants',
    ]

    for thread in threads:
        content = get_clean_thread_content(thread)
        content_lower = content.lower()
        direction = thread.get('direction')

        if direction == 'out':
            # Vérifier nos messages sortants
            for pattern in outgoing_patterns:
                if re.search(pattern, content_lower, re.IGNORECASE):
                    logger.info(f"🔍 Détecté: demande d'identifiants dans l'historique (message sortant)")
                    logger.info(f"   Pattern trouvé: {pattern}")
                    return True

        elif direction == 'in':
            # Vérifier si le candidat mentionne avoir reçu une demande d'identifiants
            for pattern in incoming_patterns:
                if re.search(pattern, content_lower, re.IGNORECASE):
                    logger.info(f"🔍 Détecté: le candidat mentionne une demande d'identifiants")
                    logger.info(f"   Pattern trouvé: {pattern}")
                    return True

    return False


def generate_account_creation_followup_response() -> str:
    """
    Génère le message à envoyer au candidat quand on lui avait précédemment
    demandé de créer son compte ExamT3P et qu'on n'a toujours pas ses identifiants.
    """
    return f"""Bonjour,

Suite à notre précédent échange, nous souhaitions savoir si vous avez pu créer votre compte sur la plateforme ExamT3P.

Si vous avez créé votre compte, merci de nous transmettre vos identifiants de connexion (email et mot de passe) afin que nous puissions assurer le suivi de votre dossier et vérifier que votre inscription est bien complète.

Si vous n'avez pas encore créé votre compte, voici les étapes à suivre :

1. Rendez-vous sur : {EXAMT3P_LOGIN_URL}
2. Cliquez sur "S'inscrire"
3. Complétez le formulaire d'inscription
4. Une fois inscrit, transmettez-nous vos identifiants par retour de mail

⚠️ **Important** : La création du compte ExamT3P est obligatoire pour pouvoir être inscrit à l'examen VTC auprès de la CMA.

En attendant votre retour,

Cordialement,
L'équipe DOC"""


def generate_credentials_request_followup_response(include_session_preference: str = None) -> str:
    """
    Génère le message à envoyer au candidat quand on lui avait précédemment
    demandé ses identifiants ExamT3P et qu'on ne les a toujours pas reçus.

    Ce message:
    1. Rassure le candidat sur le fait que c'est normal
    2. Explique pourquoi on a besoin des identifiants
    3. Demande les identifiants
    4. Inclut la procédure de création de compte au cas où
    """
    session_note = ""
    if include_session_preference:
        session_note = f"\n\nNous avons bien noté votre préférence pour les **{include_session_preference}**. Nous pourrons vous proposer les dates de formation adaptées dès que nous aurons accès à votre dossier.\n"

    return f"""Bonjour,
{session_note}
Concernant votre question : **oui, c'est tout à fait normal que notre équipe vous demande vos identifiants ExamT3P**.

**Pourquoi avons-nous besoin de vos identifiants ?**

Sans accès à votre compte ExamT3P, il nous est **impossible** de :
- Effectuer le suivi de votre dossier auprès de la CMA
- Vérifier l'état de votre inscription à l'examen
- Procéder au paiement de vos frais d'examen (si ce n'est pas encore fait)
- Vous inscrire à une date d'examen

**📝 Merci de nous transmettre vos identifiants de connexion ExamT3P :**
- **Identifiant** (généralement votre adresse email)
- **Mot de passe**

---

**Vous n'avez pas encore créé votre compte ExamT3P ?**

Pas de souci ! Voici comment faire :
1. Rendez-vous sur : {EXAMT3P_LOGIN_URL}
2. Cliquez sur "S'inscrire"
3. Complétez le formulaire d'inscription avec vos informations personnelles
4. Une fois inscrit, transmettez-nous vos identifiants par retour de mail

---

⚠️ **Conseil de sécurité** : Vérifiez toujours que les emails que vous recevez proviennent bien de @cab-formations.fr. En cas de doute, vous pouvez nous contacter directement au 01 74 90 20 82.

Dès réception de vos identifiants, nous pourrons finaliser votre dossier et vous proposer les prochaines dates d'examen disponibles.

Cordialement,
L'équipe DOC"""
