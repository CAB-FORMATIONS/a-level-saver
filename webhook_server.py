#!/usr/bin/env python3
"""
Zoho Desk Webhook Server
Receives webhook events from Zoho Desk and triggers DOCTicketWorkflow
"""

import os
import json
import hmac
import hashlib
import logging
from typing import Dict, Any, Optional
from flask import Flask, request, jsonify
from datetime import datetime
import traceback

from src.workflows.doc_ticket_workflow import DOCTicketWorkflow
from src.constants.departments import DEPT_DOC
from src.utils.logging_config import setup_logging

# Setup logging
setup_logging()
logger = logging.getLogger(__name__)

# Initialize Flask app
app = Flask(__name__)

# Configuration
WEBHOOK_SECRET = os.getenv('ZOHO_WEBHOOK_SECRET', '')


def verify_webhook_signature(payload: bytes, signature: str) -> bool:
    """
    Verify HMAC-SHA256 signature from Zoho webhook.

    Args:
        payload: Raw request body as bytes
        signature: Signature from X-Zoho-Signature header

    Returns:
        True if signature is valid, False otherwise
    """
    if not WEBHOOK_SECRET:
        logger.warning("ZOHO_WEBHOOK_SECRET not configured - skipping signature verification")
        return True

    if not signature:
        logger.warning("No signature provided in request headers")
        return False

    try:
        computed_signature = hmac.new(
            WEBHOOK_SECRET.encode('utf-8'),
            payload,
            hashlib.sha256
        ).hexdigest()

        is_valid = hmac.compare_digest(computed_signature, signature)

        if not is_valid:
            logger.error("Signature verification failed")
            logger.debug(f"Expected: {computed_signature[:8]}...")
            logger.debug(f"Received: {signature[:8]}...")

        return is_valid

    except Exception as e:
        logger.error(f"Error verifying signature: {str(e)}")
        return False


def extract_ticket_id_from_payload(data: Dict[str, Any]) -> Optional[str]:
    """
    Extract ticket ID from Zoho webhook payload.

    Zoho can send different payload structures depending on event type.
    Common patterns:
    - data['ticket']['id']
    - data['id']
    - data['entityId']
    """
    if 'ticket' in data and isinstance(data['ticket'], dict):
        return data['ticket'].get('id')

    if 'id' in data:
        return data['id']

    if 'entityId' in data:
        return data['entityId']

    if 'data' in data:
        if isinstance(data['data'], dict):
            return extract_ticket_id_from_payload(data['data'])

    return None


def extract_department_from_payload(data: Dict[str, Any]) -> Optional[str]:
    """
    Extract department name from Zoho webhook payload.

    Returns department name or None if not found.
    """
    if 'ticket' in data and isinstance(data['ticket'], dict):
        ticket = data['ticket']
        if 'department' in ticket and isinstance(ticket['department'], dict):
            return ticket['department'].get('name')
        return ticket.get('departmentId')

    if 'department' in data and isinstance(data['department'], dict):
        return data['department'].get('name')

    return data.get('departmentId')


def parse_webhook_event(data: Dict[str, Any]) -> Dict[str, Any]:
    """
    Parse webhook event data and extract relevant information.
    """
    event_info = {
        'event_type': data.get('event_type') or data.get('eventType') or 'unknown',
        'ticket_id': extract_ticket_id_from_payload(data),
        'department': extract_department_from_payload(data),
        'timestamp': data.get('timestamp') or datetime.utcnow().isoformat(),
        'org_id': data.get('orgId'),
        'raw_data': data
    }

    logger.info(f"Parsed webhook event: {event_info['event_type']} for ticket {event_info['ticket_id']} (dept: {event_info['department']})")

    return event_info


@app.route('/health', methods=['GET'])
def health_check():
    """Health check endpoint"""
    return jsonify({
        'status': 'healthy',
        'service': 'a-level-saver-webhook',
        'timestamp': datetime.utcnow().isoformat()
    })


@app.route('/webhook/zoho-desk', methods=['POST'])
def handle_zoho_desk_webhook():
    """
    Main webhook endpoint for Zoho Desk events.

    Processes DOC department tickets via DOCTicketWorkflow.
    auto_send=True by default — the internal _can_auto_send() guard rail
    decides: whitelisted scenarios → send, everything else → draft.
    """
    start_time = datetime.utcnow()

    # Get raw payload for signature verification
    raw_payload = request.get_data()
    signature = request.headers.get('X-Zoho-Signature', '')

    logger.info(f"Received webhook request from {request.remote_addr}")

    # Verify signature
    if not verify_webhook_signature(raw_payload, signature):
        logger.error("Webhook signature verification failed")
        return jsonify({
            'success': False,
            'error': 'Invalid signature'
        }), 401

    # Parse JSON payload
    try:
        data = request.get_json(force=True)
    except Exception as e:
        logger.error(f"Failed to parse JSON payload: {str(e)}")
        return jsonify({
            'success': False,
            'error': 'Invalid JSON payload'
        }), 400

    # Parse event data
    try:
        event_info = parse_webhook_event(data)
    except Exception as e:
        logger.error(f"Failed to parse webhook event: {str(e)}")
        return jsonify({
            'success': False,
            'error': 'Failed to parse event data'
        }), 400

    # Validate ticket ID
    ticket_id = event_info['ticket_id']
    if not ticket_id:
        logger.error("No ticket ID found in webhook payload")
        logger.debug(f"Payload: {json.dumps(data, indent=2)}")
        return jsonify({
            'success': False,
            'error': 'No ticket ID found in payload'
        }), 400

    # Department filter: only process DOC tickets
    dept = event_info.get('department')
    if dept and dept != DEPT_DOC:
        logger.info(f"Skipping ticket {ticket_id} — department '{dept}' is not {DEPT_DOC}")
        return jsonify({
            'success': True,
            'ticket_id': ticket_id,
            'skipped': True,
            'reason': f"Department '{dept}' is not {DEPT_DOC}"
        }), 200

    logger.info(f"Processing webhook for ticket {ticket_id}, event: {event_info['event_type']}")

    # Process ticket with DOCTicketWorkflow
    try:
        workflow = DOCTicketWorkflow()
        result = workflow.process_ticket(
            ticket_id=ticket_id,
            auto_create_draft=True,
            auto_update_crm=True,
            auto_update_ticket=True,
            auto_send=True
        )

        processing_time = (datetime.utcnow() - start_time).total_seconds()

        logger.info(f"Webhook processed in {processing_time:.2f}s — stage: {result.get('workflow_stage')}, delivery: {result.get('delivery_method')}")

        return jsonify({
            'success': result.get('success', False),
            'ticket_id': ticket_id,
            'event_type': event_info['event_type'],
            'processing_time_seconds': processing_time,
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
        logger.error(f"Error processing webhook: {str(e)}")
        logger.error(traceback.format_exc())

        return jsonify({
            'success': False,
            'ticket_id': ticket_id,
            'error': str(e),
            'error_type': type(e).__name__
        }), 500


@app.route('/webhook/test', methods=['POST'])
def test_webhook():
    """
    Test endpoint for manual webhook testing without signature verification.

    Usage:
        curl -X POST http://localhost:5000/webhook/test \
          -H "Content-Type: application/json" \
          -d '{"ticket_id": "198709000438366101"}'

    Optional fields:
        auto_send: bool (default True)
        auto_create_draft: bool (default True)
        auto_update_crm: bool (default True)
        auto_update_ticket: bool (default True)
    """
    try:
        data = request.get_json(force=True)
        ticket_id = data.get('ticket_id')

        if not ticket_id:
            return jsonify({
                'success': False,
                'error': 'ticket_id required'
            }), 400

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
        logger.error(f"Test webhook error: {str(e)}")
        logger.error(traceback.format_exc())
        return jsonify({
            'success': False,
            'error': str(e)
        }), 500


@app.route('/webhook/stats', methods=['GET'])
def webhook_stats():
    """Get webhook configuration and status."""
    return jsonify({
        'service': 'a-level-saver-webhook',
        'status': 'running',
        'configuration': {
            'auto_create_draft': True,
            'auto_update_crm': True,
            'auto_update_ticket': True,
            'auto_send': True,
            'auto_send_note': 'Guarded by _can_auto_send() — only whitelisted scenarios send directly',
            'signature_verification': bool(WEBHOOK_SECRET)
        },
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
    logger.error(f"Internal server error: {str(error)}")
    return jsonify({
        'success': False,
        'error': 'Internal server error'
    }), 500


if __name__ == '__main__':
    host = os.getenv('WEBHOOK_HOST', '0.0.0.0')
    port = int(os.getenv('WEBHOOK_PORT', '5000'))
    debug = os.getenv('FLASK_DEBUG', 'false').lower() == 'true'

    logger.info("=" * 60)
    logger.info("A-Level Saver Webhook Server Starting")
    logger.info("=" * 60)
    logger.info(f"Host: {host}")
    logger.info(f"Port: {port}")
    logger.info(f"Debug: {debug}")
    logger.info(f"Signature Verification: {'Enabled' if WEBHOOK_SECRET else 'Disabled (WARNING!)'}")
    logger.info("=" * 60)

    if not WEBHOOK_SECRET:
        logger.warning("ZOHO_WEBHOOK_SECRET not set - signature verification disabled!")
        logger.warning("This is INSECURE for production use!")

    app.run(
        host=host,
        port=port,
        debug=debug
    )
