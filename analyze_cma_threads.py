"""
Analyze CMA ticket threads from Zoho Desk.
Reads thread content for 10 CMA tickets and outputs analysis.
"""

import re
import sys
import os
from datetime import datetime

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.zoho_client import ZohoDeskClient

OUTPUT_PATH = r"C:\Users\fouad\AppData\Local\Temp\claude\C--Users-fouad-Documents-a-level-saver\df7c6d26-b26a-45d2-82e4-b249a4418bd9\scratchpad\cma_threads_analysis.txt"

TICKETS = [
    {"id": "198709000451400196", "number": "#1108975", "from": "examentaxivtcpaca@cmar-paca.fr"},
    {"id": "198709000451198576", "number": "#1108208", "from": "examentaxivtcpaca@cmar-paca.fr"},
    {"id": "198709000451209377", "number": "#1108235", "from": "n.sers@cm-tarn.fr"},
    {"id": "198709000451217770", "number": "#1108359", "from": "taxi.vtc.67@cm-alsace.fr"},
    {"id": "198709000451274271", "number": "#1108477", "from": "cmar-examens-t3p@cma-nouvelleaquitaine.fr"},
    {"id": "198709000450256658", "number": "#1103894", "from": "christel.gustin@cma-auvergnerhonealpes.fr"},
    {"id": "198709000448652257", "number": "#1097827", "from": "examentaxivtcpaca@cmar-paca.fr"},
    {"id": "198709000448354937", "number": "#1096658", "from": "f.florentin@cma-hautsdefrance.fr"},
    {"id": "198709000448311991", "number": "#1096558", "from": "j.bourguignon@cma-gard.fr"},
    {"id": "198709000451052553", "number": "#1107699", "from": "cmar-examens-t3p@cma-nouvelleaquitaine.fr"},
]


def strip_html(html_content):
    """Remove HTML tags and decode entities."""
    if not html_content:
        return ""
    # Remove style and script blocks
    text = re.sub(r'<style[^>]*>.*?</style>', '', html_content, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<script[^>]*>.*?</script>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Replace <br>, <p>, <div> with newlines
    text = re.sub(r'<br\s*/?\s*>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</div>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</tr>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</td>', ' | ', text, flags=re.IGNORECASE)
    text = re.sub(r'</th>', ' | ', text, flags=re.IGNORECASE)
    # Remove all remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Decode common HTML entities
    text = text.replace('&nbsp;', ' ')
    text = text.replace('&amp;', '&')
    text = text.replace('&lt;', '<')
    text = text.replace('&gt;', '>')
    text = text.replace('&quot;', '"')
    text = text.replace('&#39;', "'")
    text = text.replace('&rsquo;', "'")
    text = text.replace('&lsquo;', "'")
    text = text.replace('&rdquo;', '"')
    text = text.replace('&ldquo;', '"')
    text = text.replace('&eacute;', 'e')
    text = text.replace('&egrave;', 'e')
    text = text.replace('&agrave;', 'a')
    text = text.replace('&ccedil;', 'c')
    text = text.replace('&ecirc;', 'e')
    text = text.replace('&ocirc;', 'o')
    text = text.replace('&ucirc;', 'u')
    text = text.replace('&iuml;', 'i')
    # Collapse multiple whitespace/newlines
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = re.sub(r' +', ' ', text)
    return text.strip()


def main():
    client = ZohoDeskClient()

    output_lines = []

    def log(msg):
        print(msg)
        output_lines.append(msg)

    log("=" * 100)
    log("CMA TICKETS THREAD ANALYSIS")
    log(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 100)

    for i, ticket_info in enumerate(TICKETS, 1):
        ticket_id = ticket_info["id"]
        ticket_num = ticket_info["number"]
        expected_from = ticket_info["from"]

        log("")
        log("=" * 100)
        log(f"TICKET {i}/10: {ticket_num} (ID: {ticket_id})")
        log(f"Expected sender: {expected_from}")
        log("=" * 100)

        try:
            threads = client.get_all_threads_with_full_content(ticket_id)

            if not threads:
                log("  [NO THREADS FOUND]")
                continue

            log(f"  Total threads: {len(threads)}")
            log("")

            for j, thread in enumerate(threads, 1):
                from_email = thread.get("fromEmailAddress", thread.get("from", "N/A"))
                direction = thread.get("direction", "N/A")
                created_time = thread.get("createdTime", "N/A")
                summary = thread.get("summary", "")
                content = thread.get("content", "") or thread.get("htmlContent", "") or summary
                channel = thread.get("channel", "N/A")
                to_email = thread.get("to", "N/A")
                cc_email = thread.get("cc", "N/A")

                plain_text = strip_html(content)

                log(f"  --- Thread {j}/{len(threads)} ---")
                log(f"  From: {from_email}")
                log(f"  To: {to_email}")
                if cc_email and cc_email != "N/A":
                    log(f"  CC: {cc_email}")
                log(f"  Direction: {direction}")
                log(f"  Channel: {channel}")
                log(f"  Date: {created_time}")
                log(f"  Content ({len(plain_text)} chars, showing first 1500):")
                log(f"  {'~' * 80}")
                # Show first 1500 chars with indentation
                content_preview = plain_text[:1500]
                for line in content_preview.split('\n'):
                    log(f"    {line}")
                if len(plain_text) > 1500:
                    log(f"    [...TRUNCATED, {len(plain_text) - 1500} more chars...]")
                log(f"  {'~' * 80}")
                log("")

        except Exception as e:
            log(f"  [ERROR] Failed to fetch threads: {e}")
            import traceback
            log(f"  {traceback.format_exc()}")

    log("")
    log("=" * 100)
    log("END OF ANALYSIS")
    log("=" * 100)

    # Write to file
    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, 'w', encoding='utf-8') as f:
        f.write('\n'.join(output_lines))

    print(f"\n\nOutput saved to: {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
