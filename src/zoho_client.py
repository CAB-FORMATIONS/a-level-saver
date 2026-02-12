"""Zoho API client with OAuth2 authentication."""
import logging
import threading
import time
from typing import Dict, Any, Optional, List
import requests
from datetime import datetime
# Note: tenacity removed - using custom retry logic for better rate limit handling
from config import settings
from src.zoho_token_manager import get_token_manager, ZohoRateLimitError

logger = logging.getLogger(__name__)


class ZohoAPIClient:
    """Base client for Zoho API interactions with OAuth2 authentication."""

    # Class-level rate limiting for API calls (shared across all instances)
    _api_lock = threading.Lock()
    _last_api_call_time: float = 0
    MIN_API_INTERVAL = 0.3  # 300ms minimum between API calls

    def __init__(self):
        self.access_token: Optional[str] = None
        self._session = requests.Session()
        # Disable proxy for Zoho API calls
        self._session.proxies = {"http": None, "https": None}
        # Token management is now delegated to TokenManager singleton
        self._token_manager = get_token_manager()

    def _apply_api_rate_limit(self) -> None:
        """
        Apply rate limiting between API calls to prevent hitting Zoho limits.

        Uses a class-level lock to ensure rate limiting across all client instances.
        """
        with self._api_lock:
            elapsed = time.time() - self._last_api_call_time
            if elapsed < self.MIN_API_INTERVAL:
                sleep_time = self.MIN_API_INTERVAL - elapsed
                time.sleep(sleep_time)
            ZohoAPIClient._last_api_call_time = time.time()

    def _get_credentials(self) -> tuple:
        """
        Get OAuth credentials for this client.

        Override in subclasses to use different credentials.

        Returns:
            Tuple of (client_id, client_secret, refresh_token, accounts_url)
        """
        return (
            settings.zoho_client_id,
            settings.zoho_client_secret,
            settings.zoho_refresh_token,
            settings.zoho_accounts_url
        )

    def _ensure_valid_token(self) -> None:
        """
        Ensure we have a valid access token before making API calls.

        Delegates to TokenManager singleton for centralized token management.
        """
        client_id, client_secret, refresh_token, accounts_url = self._get_credentials()
        self.access_token = self._token_manager.get_token(
            client_id=client_id,
            client_secret=client_secret,
            refresh_token=refresh_token,
            accounts_url=accounts_url
        )

    def _make_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        _retry_count: int = 0,
        **kwargs
    ) -> Dict[str, Any]:
        """
        Make an authenticated API request to Zoho.

        Features:
        - Rate limiting (300ms between calls)
        - Auto-retry on 401 with token invalidation
        - Exponential backoff on 429 rate limit
        """
        MAX_RETRIES = 3

        # Apply rate limiting before the call
        self._apply_api_rate_limit()

        # Ensure valid token
        self._ensure_valid_token()

        if headers is None:
            headers = {}

        headers["Authorization"] = f"Zoho-oauthtoken {self.access_token}"
        headers["Content-Type"] = "application/json"

        try:
            response = self._session.request(
                method, url, headers=headers, timeout=30, **kwargs
            )

            # Handle 401 Unauthorized - token may be revoked
            if response.status_code == 401:
                if _retry_count < MAX_RETRIES:
                    logger.warning(f"401 Unauthorized - invalidating token and retrying...")
                    # Invalidate the cached token
                    client_id, _, refresh_token, _ = self._get_credentials()
                    self._token_manager.invalidate(client_id, refresh_token)
                    # Retry with fresh token
                    return self._make_request(
                        method, url, headers=None, _retry_count=_retry_count + 1, **kwargs
                    )

            # Handle 429 Too Many Requests - rate limited
            if response.status_code == 429:
                if _retry_count < MAX_RETRIES:
                    retry_after = int(response.headers.get("Retry-After", 60))
                    logger.warning(f"429 Rate Limited - waiting {retry_after}s before retry...")
                    time.sleep(retry_after)
                    return self._make_request(
                        method, url, headers=None, _retry_count=_retry_count + 1, **kwargs
                    )

            # Log detailed error info
            if response.status_code >= 400:
                logger.error(f"API Error {response.status_code}: {method} {url}")
                logger.error(f"Response body: {response.text[:1000]}")
                if 'json' in kwargs:
                    logger.error(f"Request payload size: {len(str(kwargs['json']))} chars")

            response.raise_for_status()

            # Handle empty responses (204 No Content or empty body)
            if response.status_code == 204 or not response.text.strip():
                return {}

            return response.json()

        except requests.exceptions.Timeout as e:
            logger.error(f"API request timeout: {method} {url}")
            if _retry_count < MAX_RETRIES:
                wait_time = 2 ** _retry_count  # Exponential backoff
                logger.info(f"Retrying after {wait_time}s...")
                time.sleep(wait_time)
                return self._make_request(
                    method, url, headers=None, _retry_count=_retry_count + 1, **kwargs
                )
            raise

        except requests.exceptions.RequestException as e:
            logger.error(f"API request failed: {method} {url} - {e}")
            if _retry_count < MAX_RETRIES:
                wait_time = 2 ** _retry_count  # Exponential backoff
                logger.info(f"Retrying after {wait_time}s...")
                time.sleep(wait_time)
                return self._make_request(
                    method, url, headers=None, _retry_count=_retry_count + 1, **kwargs
                )
            raise

    def close(self) -> None:
        """Close the session."""
        self._session.close()


class ZohoDeskClient(ZohoAPIClient):
    """Client for Zoho Desk API operations."""

    def _get_all_pages(
        self,
        url: str,
        params: Optional[Dict[str, Any]] = None,
        limit_per_page: int = 100
    ) -> List[Dict[str, Any]]:
        """
        Helper method to automatically fetch all pages from a paginated endpoint.

        Args:
            url: API endpoint URL
            params: Query parameters
            limit_per_page: Number of items per page (max 100 for Zoho)

        Returns:
            List of all items across all pages
        """
        all_items = []
        from_index = 0

        if params is None:
            params = {}

        while True:
            # Update pagination parameters
            params["from"] = from_index
            params["limit"] = limit_per_page

            logger.info(f"Fetching page starting at index {from_index}")

            response = self._make_request("GET", url, params=params)

            # Get items from response
            items = response.get("data", [])

            if not items:
                # No more items, we're done
                break

            all_items.extend(items)

            # Check if there are more pages
            # Zoho typically returns less items than limit when it's the last page
            if len(items) < limit_per_page:
                # Last page reached
                break

            # Move to next page
            from_index += len(items)

            logger.info(f"Retrieved {len(items)} items. Total so far: {len(all_items)}")

        logger.info(f"Pagination complete. Total items retrieved: {len(all_items)}")
        return all_items

    def get_ticket(self, ticket_id: str) -> Dict[str, Any]:
        """Get a specific ticket by ID."""
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}"
        params = {"orgId": settings.zoho_desk_org_id}
        return self._make_request("GET", url, params=params)

    def list_tickets(
        self,
        status: Optional[str] = None,
        limit: int = 50,
        from_index: int = 0
    ) -> Dict[str, Any]:
        """
        List tickets with optional filters (single page).

        For fetching ALL tickets, use list_all_tickets() instead.
        """
        url = f"{settings.zoho_desk_api_url}/tickets"
        params = {
            "orgId": settings.zoho_desk_org_id,
            "limit": limit,
            "from": from_index
        }
        if status:
            params["status"] = status

        return self._make_request("GET", url, params=params)

    def list_all_tickets(
        self,
        status: Optional[str] = None,
        limit_per_page: int = 100
    ) -> List[Dict[str, Any]]:
        """
        List ALL tickets with automatic pagination.

        This method automatically fetches all pages and returns all tickets.

        Args:
            status: Optional status filter (e.g., "Open", "Pending", "Closed")
            limit_per_page: Items per page (max 100)

        Returns:
            List of all tickets
        """
        url = f"{settings.zoho_desk_api_url}/tickets"
        params = {"orgId": settings.zoho_desk_org_id}

        if status:
            params["status"] = status

        return self._get_all_pages(url, params, limit_per_page)

    def list_departments(self) -> List[Dict[str, Any]]:
        """
        List all departments in Zoho Desk (with pagination).

        Returns:
            List of all departments with id, name, etc.
        """
        url = f"{settings.zoho_desk_api_url}/departments"
        params = {"orgId": settings.zoho_desk_org_id}
        return self._get_all_pages(url, params, limit_per_page=100)

    def get_department_id_by_name(self, name: str) -> Optional[str]:
        """
        Get department ID by name.

        Args:
            name: Department name (e.g., "Contact", "DOC", "Refus CMA")

        Returns:
            Department ID as string, or None if not found
        """
        departments = self.list_departments()
        for dept in departments:
            if dept.get("name", "").lower() == name.lower():
                return str(dept.get("id"))
        return None

    def get_department_info(self, name: str) -> Optional[Dict[str, Any]]:
        """
        Get full department info by name.

        Args:
            name: Department name

        Returns:
            Department dict with id, name, layoutId, etc.
        """
        departments = self.list_departments()
        for dept in departments:
            if dept.get("name", "").lower() == name.lower():
                return dept
        return None

    def move_ticket_to_department(
        self,
        ticket_id: str,
        department_name: str
    ) -> Dict[str, Any]:
        """
        Move a ticket to a different department.

        Uses the dedicated /move endpoint which is the proper way
        to transfer tickets between departments in Zoho Desk.

        Args:
            ticket_id: Ticket ID to move
            department_name: Target department name

        Returns:
            Updated ticket data
        """
        # Get department info
        dept_info = self.get_department_info(department_name)
        if not dept_info:
            raise ValueError(f"Department '{department_name}' not found")

        dept_id = dept_info.get("id")

        logger.info(f"Moving ticket {ticket_id} to department {department_name} (ID: {dept_id})")

        # Use the dedicated /move endpoint (POST, not PATCH)
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}/move"
        params = {"orgId": settings.zoho_desk_org_id}
        data = {"departmentId": str(dept_id)}

        return self._make_request("POST", url, params=params, json=data)

    def update_ticket(
        self,
        ticket_id: str,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update a ticket."""
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}"
        params = {"orgId": settings.zoho_desk_org_id}
        return self._make_request("PATCH", url, params=params, json=data)

    def get_ticket_comments(
        self,
        ticket_id: str,
        include_public: bool = True,
        include_private: bool = True
    ) -> List[Dict[str, Any]]:
        """Get all comments for a ticket.

        Args:
            ticket_id: The ticket ID
            include_public: Include public comments
            include_private: Include private/internal comments

        Returns:
            List of comment objects
        """
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}/comments"
        params = {"orgId": settings.zoho_desk_org_id}
        response = self._make_request("GET", url, params=params)

        comments = response.get('data', [])

        # Filter by visibility if needed
        if not include_public:
            comments = [c for c in comments if not c.get('isPublic', True)]
        if not include_private:
            comments = [c for c in comments if c.get('isPublic', True)]

        return comments

    def add_ticket_comment(
        self,
        ticket_id: str,
        content: str,
        is_public: bool = True
    ) -> Dict[str, Any]:
        """Add a comment to a ticket."""
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}/comments"
        params = {"orgId": settings.zoho_desk_org_id}
        data = {
            "content": content,
            "isPublic": is_public
        }
        return self._make_request("POST", url, params=params, json=data)

    def create_ticket_reply_draft(
        self,
        ticket_id: str,
        content: str,
        content_type: str = "plainText",
        from_email: str = None,
        to_email: str = None
    ) -> Dict[str, Any]:
        """
        Create a draft reply for a ticket.

        This creates a draft email reply that can be reviewed and edited
        before being sent to the customer.

        Args:
            ticket_id: The ticket ID
            content: The draft content (HTML or plain text)
            content_type: "html" or "plainText" (default: "plainText")
            from_email: Sender email address (optional)
            to_email: Recipient email address (optional)

        Returns:
            Dict containing the draft thread details

        Documentation:
            https://desk.zoho.com/DeskAPIDocument#Threads#Threads_CreateDraft
        """
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}/draftReply"
        params = {"orgId": settings.zoho_desk_org_id}
        data = {
            "channel": "EMAIL",
            "contentType": content_type,
            "content": content,
            "isForward": False
        }
        # Ajouter fromEmailAddress si fourni
        if from_email:
            data["fromEmailAddress"] = from_email
        # Ajouter to si fourni
        if to_email:
            data["to"] = to_email

        logger.info(f"Creating draft reply for ticket {ticket_id}")
        logger.debug(f"Draft payload: channel=EMAIL, contentType={content_type}, content_length={len(content)}")
        return self._make_request("POST", url, params=params, json=data)

    def send_ticket_reply(
        self,
        ticket_id: str,
        content: str,
        content_type: str = "html",
        from_email: str = None,
        to_email: str = None
    ) -> Dict[str, Any]:
        """
        Send a reply on a ticket (sends immediately, NOT a draft).

        Used by the automated workflow when auto_send=True, with guard rails
        in _can_auto_send() ensuring the response is safe before sending.
        Falls back to create_ticket_reply_draft() if guard rails fail.

        Args:
            ticket_id: The ticket ID
            content: The reply content (HTML or plain text)
            content_type: "html" or "plainText" (default: "html")
            from_email: Sender email address (optional)
            to_email: Recipient email address (optional)

        Returns:
            Dict containing the sent thread details
        """
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}/sendReply"
        params = {"orgId": settings.zoho_desk_org_id}
        data = {
            "channel": "EMAIL",
            "contentType": content_type,
            "content": content,
            "isForward": False
        }
        if from_email:
            data["fromEmailAddress"] = from_email
        if to_email:
            data["to"] = to_email

        logger.info(f"Sending reply for ticket {ticket_id}")
        return self._make_request("POST", url, params=params, json=data)

    def has_existing_draft(self, ticket_id: str) -> bool:
        """
        Check if a ticket already has a draft reply.

        Args:
            ticket_id: The ticket ID

        Returns:
            True if a draft exists, False otherwise
        """
        try:
            threads_response = self.get_ticket_threads(ticket_id)
            threads = threads_response.get('data', []) if isinstance(threads_response, dict) else threads_response

            for thread in threads:
                # Les brouillons ont status="DRAFT" (majuscules)
                status = thread.get('status', '').upper()
                if status == 'DRAFT':
                    logger.info(f"Draft existant trouvé pour ticket {ticket_id}")
                    return True

            return False
        except Exception as e:
            logger.warning(f"Erreur vérification draft pour ticket {ticket_id}: {e}")
            return False

    def get_ticket_threads(self, ticket_id: str) -> Dict[str, Any]:
        """
        Get all threads (emails, replies) for a ticket.
        This returns the list of threads.

        WARNING: This may return summaries only. Use get_all_threads_with_full_content()
        to ensure you get the complete email body for each thread.
        """
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}/threads"
        params = {"orgId": settings.zoho_desk_org_id}
        return self._make_request("GET", url, params=params)

    def get_thread_details(self, ticket_id: str, thread_id: str) -> Dict[str, Any]:
        """
        Get the complete details of a specific thread including full content.

        This endpoint retrieves the full email body for a single thread,
        not just a summary.

        Args:
            ticket_id: The ticket ID
            thread_id: The thread ID to fetch

        Returns:
            Complete thread data with full content
        """
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}/threads/{thread_id}"
        params = {"orgId": settings.zoho_desk_org_id}
        return self._make_request("GET", url, params=params)

    def get_all_threads_with_full_content(self, ticket_id: str) -> List[Dict[str, Any]]:
        """
        Get all threads for a ticket with FULL content for each thread.

        This method ensures you get complete email bodies, not summaries:
        1. Gets the list of threads
        2. Fetches full details for each thread individually
        3. Returns complete thread data with full email content

        This is the recommended method to use when you need the complete
        email conversation history.

        Args:
            ticket_id: The ticket ID

        Returns:
            List of threads with full content
        """
        logger.info(f"Fetching all threads with full content for ticket {ticket_id}")

        # Get list of threads
        threads_response = self.get_ticket_threads(ticket_id)
        threads_list = threads_response.get("data", [])

        if not threads_list:
            logger.info(f"No threads found for ticket {ticket_id}")
            return []

        # Fetch full details for each thread
        full_threads = []
        for idx, thread in enumerate(threads_list, 1):
            thread_id = thread.get("id")
            if thread_id:
                try:
                    # Get full thread details with complete content
                    full_thread = self.get_thread_details(ticket_id, thread_id)
                    full_threads.append(full_thread)
                    logger.debug(f"Fetched full content for thread {thread_id} ({idx}/{len(threads_list)})")
                except Exception as e:
                    logger.warning(f"Could not fetch full details for thread {thread_id}: {e}")
                    # Fallback to the summary data from the list
                    logger.warning(f"Using summary data for thread {thread_id} (may be incomplete)")
                    full_threads.append(thread)
            else:
                logger.warning(f"Thread without ID found, using as-is")
                full_threads.append(thread)

        logger.info(f"Fetched {len(full_threads)} threads with full content for ticket {ticket_id}")
        return full_threads

    def get_ticket_conversations(self, ticket_id: str) -> Dict[str, Any]:
        """
        Get all conversations for a ticket.
        This includes all interactions and communications.
        """
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}/conversations"
        params = {"orgId": settings.zoho_desk_org_id}
        return self._make_request("GET", url, params=params)

    def get_ticket_history(self, ticket_id: str) -> Dict[str, Any]:
        """
        Get the complete history of a ticket.
        This includes all modifications, status changes, assignments, etc.
        """
        url = f"{settings.zoho_desk_api_url}/tickets/{ticket_id}/history"
        params = {"orgId": settings.zoho_desk_org_id}
        return self._make_request("GET", url, params=params)

    def get_ticket_complete_context(self, ticket_id: str) -> Dict[str, Any]:
        """
        Get complete context for a ticket including:
        - Basic ticket information
        - Full thread history with COMPLETE email content (not summaries)
        - All conversations
        - Complete modification history

        This provides the most comprehensive view of a ticket for AI analysis.

        IMPORTANT: This method fetches each thread individually to ensure
        we get the full email body content, not just summaries.
        """
        logger.info(f"Fetching complete context for ticket {ticket_id}")

        # Get basic ticket info
        ticket = self.get_ticket(ticket_id)

        # Get all threads with FULL content (fetches each thread individually)
        try:
            threads = self.get_all_threads_with_full_content(ticket_id)
        except Exception as e:
            logger.warning(f"Could not fetch threads for ticket {ticket_id}: {e}")
            threads = []

        # Get conversations
        try:
            conversations_response = self.get_ticket_conversations(ticket_id)
            conversations = conversations_response.get("data", [])
        except Exception as e:
            logger.warning(f"Could not fetch conversations for ticket {ticket_id}: {e}")
            conversations = []

        # Get history
        try:
            history_response = self.get_ticket_history(ticket_id)
            history = history_response.get("data", [])
        except Exception as e:
            logger.warning(f"Could not fetch history for ticket {ticket_id}: {e}")
            history = []

        return {
            "ticket": ticket,
            "threads": threads,  # Now contains full content for each thread
            "conversations": conversations,
            "history": history
        }


class ZohoCRMClient(ZohoAPIClient):
    """Client for Zoho CRM API operations."""

    def __init__(self):
        super().__init__()
        # Store CRM-specific credentials for _get_credentials override
        self._crm_client_id = settings.zoho_crm_client_id or settings.zoho_client_id
        self._crm_client_secret = settings.zoho_crm_client_secret or settings.zoho_client_secret
        self._crm_refresh_token = settings.zoho_crm_refresh_token or settings.zoho_refresh_token

    def _get_credentials(self) -> tuple:
        """
        Get CRM-specific OAuth credentials.

        Uses separate CRM credentials if available, otherwise falls back to Desk credentials.

        Returns:
            Tuple of (client_id, client_secret, refresh_token, accounts_url)
        """
        return (
            self._crm_client_id,
            self._crm_client_secret,
            self._crm_refresh_token,
            settings.zoho_accounts_url
        )

    def get_record(self, module: str, record_id: str) -> Dict[str, Any]:
        """Get a specific record by module name and ID."""
        url = f"{settings.zoho_crm_api_url}/{module}/{record_id}"
        response = self._make_request("GET", url)
        return response.get("data", [{}])[0] if response.get("data") else {}

    def get_deal(self, deal_id: str) -> Dict[str, Any]:
        """Get a specific deal/opportunity by ID."""
        url = f"{settings.zoho_crm_api_url}/Deals/{deal_id}"
        response = self._make_request("GET", url)
        return response.get("data", [{}])[0] if response.get("data") else {}

    def get_contact(self, contact_id: str) -> Dict[str, Any]:
        """Get a specific contact by ID."""
        url = f"{settings.zoho_crm_api_url}/Contacts/{contact_id}"
        response = self._make_request("GET", url)
        return response.get("data", [{}])[0] if response.get("data") else {}

    def update_contact(
        self,
        contact_id: str,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update a contact."""
        url = f"{settings.zoho_crm_api_url}/Contacts/{contact_id}"
        payload = {"data": [data]}
        return self._make_request("PUT", url, json=payload)

    def update_deal(
        self,
        deal_id: str,
        data: Dict[str, Any]
    ) -> Dict[str, Any]:
        """Update a deal/opportunity."""
        url = f"{settings.zoho_crm_api_url}/Deals/{deal_id}"
        payload = {"data": [data]}
        return self._make_request("PUT", url, json=payload)

    def search_deals(
        self,
        criteria: str,
        page: int = 1,
        per_page: int = 200
    ) -> Dict[str, Any]:
        """
        Search for deals using criteria (single page).

        For fetching ALL matching deals, use search_all_deals() instead.
        """
        url = f"{settings.zoho_crm_api_url}/Deals/search"
        params = {
            "criteria": criteria,
            "page": page,
            "per_page": per_page
        }
        return self._make_request("GET", url, params=params)

    def search_deals_by_email(self, email: str) -> List[Dict[str, Any]]:
        """
        Search for deals by email address.

        Args:
            email: Email address to search for

        Returns:
            List of matching deals
        """
        if not email:
            return []

        criteria = f"(Email:equals:{email})"
        response = self.search_deals(criteria=criteria)
        return response.get("data", [])

    def search_all_deals(
        self,
        criteria: str,
        per_page: int = 200
    ) -> List[Dict[str, Any]]:
        """
        Search for ALL deals matching criteria with automatic pagination.

        This method automatically fetches all pages and returns all matching deals.

        Args:
            criteria: Zoho CRM search criteria string
            per_page: Items per page (max 200 for CRM)

        Returns:
            List of all matching deals
        """
        all_deals = []
        page = 1

        while True:
            logger.info(f"Searching deals - page {page}")

            response = self.search_deals(
                criteria=criteria,
                page=page,
                per_page=per_page
            )

            deals = response.get("data", [])

            if not deals:
                # No more deals
                break

            all_deals.extend(deals)

            # Check pagination info
            info = response.get("info", {})
            more_records = info.get("more_records", False)

            if not more_records:
                # No more pages
                break

            page += 1

            logger.info(f"Retrieved {len(deals)} deals. Total so far: {len(all_deals)}")

        logger.info(f"Search complete. Total deals retrieved: {len(all_deals)}")
        return all_deals

    def get_deal_notes(self, deal_id: str) -> Dict[str, Any]:
        """Get notes for a specific deal."""
        url = f"{settings.zoho_crm_api_url}/Deals/{deal_id}/Notes"
        return self._make_request("GET", url, params={"fields": "Note_Title,Note_Content,Created_Time"})

    def get_deal_timeline(self, deal_id: str) -> Dict[str, Any]:
        """Get timeline for a deal (v8 API — shows field changes and actions)."""
        base = settings.zoho_crm_api_url.replace('/v3', '/v8')
        url = f"{base}/Deals/{deal_id}/__timeline"
        return self._make_request("GET", url)

    def add_deal_note(
        self,
        deal_id: str,
        note_title: str,
        note_content: str
    ) -> Dict[str, Any]:
        """Add a note to a deal."""
        url = f"{settings.zoho_crm_api_url}/Deals/{deal_id}/Notes"
        data = {
            "data": [{
                "Note_Title": note_title,
                "Note_Content": note_content,
                "Parent_Id": {
                    "id": deal_id
                }
            }]
        }
        return self._make_request("POST", url, json=data)

    def search_contacts(
        self,
        criteria: str,
        page: int = 1,
        per_page: int = 200
    ) -> Dict[str, Any]:
        """
        Search for contacts using criteria.

        Example: search_contacts("(Email:equals:john@example.com)")
        """
        url = f"{settings.zoho_crm_api_url}/Contacts/search"
        params = {
            "criteria": criteria,
            "page": page,
            "per_page": per_page
        }
        return self._make_request("GET", url, params=params)

    def get_deals_by_contact(
        self,
        contact_id: str,
        per_page: int = 200
    ) -> List[Dict[str, Any]]:
        """
        Get all deals associated with a specific contact.

        Args:
            contact_id: The Zoho CRM Contact ID
            per_page: Items per page (max 200)

        Returns:
            List of deals associated with the contact
        """
        try:
            # Search deals where Contact_Name.id equals the contact_id
            criteria = f"(Contact_Name:equals:{contact_id})"
            result = self.search_deals(criteria=criteria, per_page=per_page)
            return result.get("data", [])
        except Exception as e:
            logger.warning(f"Error getting deals for contact {contact_id}: {e}")
            return []
