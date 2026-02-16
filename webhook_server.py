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

from src.workflows.doc_ticket_workflow import DOCTicketWorkflow
from src.utils.logging_config import setup_logging

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Configuration — shared secret sent by Deluge in X-Webhook-Secret header
WEBHOOK_SECRET = os.getenv('ZOHO_WEBHOOK_SECRET', '')


def verify_secret(req) -> bool:
    """Verify the shared secret from X-Webhook-Secret header."""
    if not WEBHOOK_SECRET:
        logger.warning("ZOHO_WEBHOOK_SECRET not configured - skipping auth")
        return True

    provided = req.headers.get('X-Webhook-Secret', '')
    if not provided:
        logger.warning("No X-Webhook-Secret header provided")
        return False

    if provided != WEBHOOK_SECRET:
        logger.error("X-Webhook-Secret mismatch")
        return False

    return True


def process_ticket_background(ticket_id: str, auto_send: bool = True):
    """Process a ticket in a background thread."""
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
            for e in errors:
                logger.error(f"[BG] Ticket {ticket_id} error: {e}")
    except Exception as e:
        logger.error(f"[BG] Ticket {ticket_id} FAILED: {e}")
        logger.error(traceback.format_exc())


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
    Test endpoint — synchronous, no auth, returns full result.

    Usage:
        curl -X POST https://.../webhook/test \
          -H "Content-Type: application/json" \
          -d '{"ticket_id": "198709000438366101"}'
    """
    try:
        data = request.get_json(force=True)
        ticket_id = data.get('ticket_id')

        if not ticket_id:
            return jsonify({'success': False, 'error': 'ticket_id required'}), 400

        logger.info(f"Test webhook triggered for ticket {ticket_id}")

        workflow = DOCTicketWorkflow()
        result = workflow.process_ticket(
            ticket_id=ticket_id,
            auto_create_draft=data.get('auto_create_draft', True),
            auto_update_crm=data.get('auto_update_crm', True),
            auto_update_ticket=data.get('auto_update_ticket', True),
            auto_send=data.get('auto_send', True)
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
            'auto_send': 'guarded by _can_auto_send()'
        },
        'active_threads': threading.active_count(),
        'timestamp': datetime.utcnow().isoformat()
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
            'GET /webhook/stats'
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
    logger.info(f"Auth: {'Enabled' if WEBHOOK_SECRET else 'DISABLED (WARNING)'}")
    logger.info("=" * 60)

    app.run(host=host, port=port, debug=debug)
