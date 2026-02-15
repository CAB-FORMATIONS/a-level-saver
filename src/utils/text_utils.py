"""Text utilities for cleaning and processing content."""
import re
import html


def clean_html_content(html_content: str) -> str:
    """
    Clean HTML content to extract plain text.

    This removes HTML tags, decodes HTML entities, and cleans up whitespace.

    Args:
        html_content: HTML content to clean

    Returns:
        Clean plain text
    """
    if not html_content:
        return ""

    # Decode HTML entities (e.g., &nbsp; -> space, &lt; -> <)
    text = html.unescape(html_content)

    # Remove HTML comments
    text = re.sub(r'<!--.*?-->', '', text, flags=re.DOTALL)

    # Remove script and style elements
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)

    # Replace <br> and </p> with newlines
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)

    # Remove all remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)

    # Clean up whitespace
    # Replace multiple spaces with single space
    text = re.sub(r' +', ' ', text)

    # Replace multiple newlines with maximum 2
    text = re.sub(r'\n\n+', '\n\n', text)

    # Strip leading/trailing whitespace
    text = text.strip()

    return text


def get_clean_thread_content(thread: dict) -> str:
    """
    Extract clean text content from a thread.

    Tries plainText first, then cleans HTML content if needed.

    Args:
        thread: Thread dictionary from Zoho Desk API

    Returns:
        Clean text content
    """
    # Prefer plainText if available
    plain_text = thread.get("plainText", "").strip()
    if plain_text:
        return plain_text

    # Fallback to cleaning HTML content
    html_content = thread.get("content", "")
    if html_content:
        return clean_html_content(html_content)

    # No content available
    return "N/A"
