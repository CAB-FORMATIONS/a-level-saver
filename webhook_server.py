#!/usr/bin/env python3
"""
Zoho Desk Webhook Server
Receives calls from Zoho Desk Deluge functions and triggers DOCTicketWorkflow.

Architecture:
  Zoho Desk Workflow Rule → Deluge invokeurl → This server → DOCTicketWorkflow
  Response: 200 immediate, processing in background thread.
"""

import os
import json
import logging
import threading
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify
from datetime import datetime
import traceback

from collections import deque
from src.workflows.doc_ticket_workflow import DOCTicketWorkflow
from src.utils.logging_config import setup_logging

# In-memory log buffer (last 2000 lines)
LOG_BUFFER = deque(maxlen=2000)


class BufferHandler(logging.Handler):
    """Captures log records into an in-memory deque."""
    def emit(self, record):
        try:
            LOG_BUFFER.append(self.format(record))
        except Exception:
            pass


# Setup logging
setup_logging()
logger = logging.getLogger(__name__)

# Attach buffer handler to root logger so ALL logs are captured
_buf_handler = BufferHandler()
_buf_handler.setLevel(logging.INFO)
_buf_handler.setFormatter(logging.Formatter(
    '%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
))
logging.getLogger().addHandler(_buf_handler)

# Initialize Flask app
app = Flask(__name__)

# Configuration — shared secret sent by Deluge in X-Webhook-Secret header
WEBHOOK_SECRET = os.getenv('ZOHO_WEBHOOK_SECRET', '')
ENABLE_LIVE_TEST_WEBHOOK = os.getenv('ENABLE_LIVE_TEST_WEBHOOK', '').lower() == 'true'
_PROCESSING_TICKETS = set()
_PENDING_TICKETS = set()
_PROCESSING_LOCK = threading.Lock()


def verify_secret(req) -> bool:
    """Verify the shared secret from X-Webhook-Secret header."""
    if not WEBHOOK_SECRET:
        logger.error("ZOHO_WEBHOOK_SECRET not configured - rejecting webhook")
        return False

    provided = req.headers.get('X-Webhook-Secret', '')
    if not provided:
        logger.warning("No X-Webhook-Secret header provided")
        return False

    if provided != WEBHOOK_SECRET:
        logger.error("X-Webhook-Secret mismatch")
        return False

    return True


def process_ticket_background(ticket_id: str, auto_send: bool = False):
    """Process a ticket in a background thread."""
    with _PROCESSING_LOCK:
        if ticket_id in _PROCESSING_TICKETS:
            _PENDING_TICKETS.add(ticket_id)
            logger.info(f"[BG] Ticket {ticket_id} already processing - rerun queued")
            return
        _PROCESSING_TICKETS.add(ticket_id)

    registration_released = False
    try:
        while True:
            try:
                logger.info(f"[BG] Starting workflow for ticket {ticket_id}")
                workflow = DOCTicketWorkflow()
                result = workflow.process_ticket(
                    ticket_id=ticket_id,
                    auto_create_draft=True,
                    auto_update_crm=True,
                    auto_update_ticket=True,
                    auto_send=auto_send
                )
                stage = result.get('workflow_stage', '?')
                delivery = result.get('delivery_method', '?')
                errors = result.get('errors', [])
                logger.info(f"[BG] Ticket {ticket_id} done — stage={stage}, delivery={delivery}, errors={len(errors)}")
                if errors:
                    for error in errors:
                        logger.error(f"[BG] Ticket {ticket_id} error: {error}")
            except Exception as exc:
                logger.error(f"[BG] Ticket {ticket_id} FAILED: {exc}")
                logger.error(traceback.format_exc())

            with _PROCESSING_LOCK:
                if ticket_id in _PENDING_TICKETS:
                    _PENDING_TICKETS.discard(ticket_id)
                    rerun = True
                else:
                    _PROCESSING_TICKETS.discard(ticket_id)
                    registration_released = True
                    rerun = False
            if not rerun:
                return
            logger.info(f"[BG] Reprocessing ticket {ticket_id} after queued event")
    finally:
        if not registration_released:
            with _PROCESSING_LOCK:
                _PROCESSING_TICKETS.discard(ticket_id)
                _PENDING_TICKETS.discard(ticket_id)


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'a-level-saver-webhook',
        'timestamp': datetime.utcnow().isoformat(),
        'active_threads': threading.active_count()
    })


@app.route('/webhook/zoho-desk', methods=['POST'])
def handle_zoho_desk_webhook():
    """
    Main endpoint called by Zoho Desk Deluge function.

    Expects JSON: {"ticket_id": "198709000..."}
    Authenticates via X-Webhook-Secret header.
    Returns 200 immediately, processes ticket in background.
    """
    # Auth check
    if not verify_secret(request):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    # Parse payload
    try:
        data = request.get_json(force=True)
    except Exception as e:
        logger.error(f"Failed to parse JSON: {e}")
        return jsonify({'success': False, 'error': 'Invalid JSON'}), 400

    ticket_id = data.get('ticket_id') or data.get('ticketId')
    if not ticket_id:
        return jsonify({'success': False, 'error': 'ticket_id required'}), 400

    ticket_id = str(ticket_id)
    logger.info(f"Received request for ticket {ticket_id} — dispatching to background")

    # Fire and forget — respond 200 immediately
    thread = threading.Thread(
        target=process_ticket_background,
        args=(ticket_id,),
        daemon=True
    )
    thread.start()

    return jsonify({
        'success': True,
        'ticket_id': ticket_id,
        'message': 'Processing in background'
    }), 200


@app.route('/webhook/test', methods=['POST'])
def test_webhook():
    """
    Synchronous live test endpoint. Authenticated and disabled by default.

    Enabling it requires ENABLE_LIVE_TEST_WEBHOOK=true. Every request must
    explicitly acknowledge that the workflow may mutate live systems.

    Usage:
        curl -X POST https://.../webhook/test \
          -H "Content-Type: application/json" \
          -H "X-Webhook-Secret: ..." \
          -d '{"ticket_id": "...", "confirm_live_mutations": true}'
    """
    if not verify_secret(request):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    if not ENABLE_LIVE_TEST_WEBHOOK:
        return jsonify({'success': False, 'error': 'Live test webhook disabled'}), 403

    try:
        data = request.get_json(force=True)
        ticket_id = data.get('ticket_id')

        if not ticket_id:
            return jsonify({'success': False, 'error': 'ticket_id required'}), 400
        if data.get('confirm_live_mutations') is not True:
            return jsonify({'success': False, 'error': 'confirm_live_mutations=true required'}), 400

        logger.info(f"Test webhook triggered for ticket {ticket_id}")

        workflow = DOCTicketWorkflow()
        result = workflow.process_ticket(
            ticket_id=ticket_id,
            auto_create_draft=data.get('auto_create_draft', False),
            auto_update_crm=data.get('auto_update_crm', False),
            auto_update_ticket=data.get('auto_update_ticket', False),
            auto_send=data.get('auto_send', False)
        )

        return jsonify({
            'success': result.get('success', False),
            'ticket_id': ticket_id,
            'result': {
                'workflow_stage': result.get('workflow_stage'),
                'delivery_method': result.get('delivery_method'),
                'draft_created': result.get('draft_created'),
                'reply_sent': result.get('reply_sent'),
                'crm_updated': result.get('crm_updated'),
                'ticket_updated': result.get('ticket_updated'),
                'skip_reason': result.get('skip_reason'),
                'errors': result.get('errors', [])
            }
        }), 200

    except Exception as e:
        logger.error(f"Test webhook error: {e}")
        logger.error(traceback.format_exc())
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/webhook/stats', methods=['GET'])
def webhook_stats():
    """Get webhook configuration and status."""
    return jsonify({
        'service': 'a-level-saver-webhook',
        'status': 'running',
        'configuration': {
            'auth': 'X-Webhook-Secret header',
            'auth_enabled': bool(WEBHOOK_SECRET),
            'processing': 'async (background thread)',
            'auto_send': 'disabled; draft-only until an audited scenario is allowlisted'
        },
        'active_threads': threading.active_count(),
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route('/logs', methods=['GET'])
def get_logs():
    """
    Return recent application logs from in-memory buffer.

    Query params:
      ?lines=200       — number of lines (default 200, max 2000)
      ?level=ERROR     — filter by level (INFO, WARNING, ERROR)
      ?q=keyword       — filter lines containing keyword (case-insensitive)
      ?format=text     — return plain text instead of JSON
    """
    if not verify_secret(request):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    lines = min(int(request.args.get('lines', 200)), 2000)
    level_filter = request.args.get('level', '').upper()
    query = request.args.get('q', '').lower()
    fmt = request.args.get('format', 'json')

    result = list(LOG_BUFFER)

    if level_filter:
        result = [l for l in result if f' - {level_filter} - ' in l]
    if query:
        result = [l for l in result if query in l.lower()]

    result = result[-lines:]

    if fmt == 'text':
        return '\n'.join(result), 200, {'Content-Type': 'text/plain; charset=utf-8'}

    return jsonify({
        'total_buffered': len(LOG_BUFFER),
        'returned': len(result),
        'filters': {'lines': lines, 'level': level_filter or None, 'query': query or None},
        'logs': result
    })


@app.route('/logs/ticket/<ticket_id>', methods=['GET'])
def get_ticket_logs(ticket_id):
    """Return logs filtered for a specific ticket ID."""
    if not verify_secret(request):
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    fmt = request.args.get('format', 'json')
    result = [l for l in LOG_BUFFER if ticket_id in l]

    if fmt == 'text':
        return '\n'.join(result), 200, {'Content-Type': 'text/plain; charset=utf-8'}

    return jsonify({
        'ticket_id': ticket_id,
        'returned': len(result),
        'logs': result
    })


@app.errorhandler(404)
def not_found(error):
    return jsonify({
        'success': False,
        'error': 'Endpoint not found',
        'available_endpoints': [
            'GET /health',
            'POST /webhook/zoho-desk',
            'POST /webhook/test',
            'GET /webhook/stats',
            'GET /logs',
            'GET /logs/ticket/<ticket_id>'
        ]
    }), 404


@app.errorhandler(500)
def internal_error(error):
    logger.error(f"Internal server error: {error}")
    return jsonify({'success': False, 'error': 'Internal server error'}), 500


if __name__ == '__main__':
    host = os.getenv('WEBHOOK_HOST', '0.0.0.0')
    port = int(os.getenv('WEBHOOK_PORT', '5000'))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'

    logger.info("=" * 60)
    logger.info("A-Level Saver Webhook Server Starting")
    logger.info("=" * 60)
    logger.info(f"Host: {host}:{port} | Debug: {debug}")
    logger.info(f"Auth: {'Enabled' if WEBHOOK_SECRET else 'MISSING (webhooks rejected)'}")
    logger.info("=" * 60)

    app.run(host=host, port=port, debug=debug)
