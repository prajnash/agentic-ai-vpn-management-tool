"""
VPN Management Tool - Cloud Run Service
Integrates with Vertex AI / Conversational Agents (Dialogflow CX).

Flat parameter design: each function has its own dedicated route so Vertex AI's
OpenAPI tool can call it directly with simple top-level string fields — no nested
objects or arrays in the request body (unsupported by the Vertex AI tool schema).
"""

import logging
import os
from datetime import datetime, timezone
from typing import Any

from flask import Flask, jsonify, request
from google.cloud import firestore

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App & Firestore
# ---------------------------------------------------------------------------
app = Flask(__name__)

_db: firestore.Client | None = None


def get_db() -> firestore.Client:
    """Lazy-initialise the Firestore client (singleton)."""
    global _db
    if _db is None:
        project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
        database = os.environ.get("FIRESTORE_DATABASE", "userentry")
        _db = firestore.Client(project=project_id, database=database)
        logger.info("Firestore client initialised (project=%s, database=%s)", project_id, database)
    return _db


# ---------------------------------------------------------------------------
# Firestore collection names
# ---------------------------------------------------------------------------
COLLECTION_VPN_ACCESS = "vpn_user_access"
COLLECTION_SUPPORT_TICKET = "vpn_support_ticket"

# ---------------------------------------------------------------------------
# Valid ticket statuses
# ---------------------------------------------------------------------------
VALID_TICKET_STATUSES = {"open", "In_Progress", "resolved", "closed", "Pending"}


# ===========================================================================
# Helper utilities
# ===========================================================================

def success_response(data: dict) -> dict:
    """Standard success envelope understood by Vertex AI agents."""
    return {"success": True, "data": data}


def error_response(message: str, code: str = "INTERNAL_ERROR") -> tuple[dict, int]:
    """Standard error envelope with HTTP status."""
    status_map = {
        "BAD_REQUEST": 400,
        "VALIDATION_ERROR": 400,
        "NOT_FOUND": 404,
        "INTERNAL_ERROR": 500,
    }
    http_status = status_map.get(code, 400)
    return {"success": False, "error": {"code": code, "message": message}}, http_status


def now_utc() -> str:
    """Return the current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


def get_json_body() -> tuple[dict | None, tuple | None]:
    """
    Parse request JSON body. Returns (body, None) on success,
    or (None, error_response_tuple) on failure.
    """
    body = request.get_json(silent=True)
    if not body:
        logger.warning("Request received with no JSON body.")
        return None, error_response("Request body must be valid JSON.", "BAD_REQUEST")
    return body, None


# ===========================================================================
# Core business logic
# ===========================================================================

def _check_vpn_user_access(employee_id: str) -> dict:
    """
    Check (and, if needed, reactivate) a user's VPN access.
    Raises ValueError on bad input.
    """
    employee_id = str(employee_id).strip()
    if not employee_id:
        raise ValueError("'employee_id' must be a non-empty string.")

    db = get_db()
    doc_ref = db.collection(COLLECTION_VPN_ACCESS).document(employee_id)
    doc = doc_ref.get()

    if not doc.exists:
        # Auto-create the user record with status inactive so IT can
        # see them in Firestore and activate when ready.
        ts_now = now_utc()
        doc_ref.set({
            "employee_id": employee_id,
            "status": "inactive",
            "created_at": ts_now,
            "updated_at": ts_now,
            "note": "Auto-created on first access check. Pending IT provisioning.",
        })
        logger.info(
            "VPN access: employee '%s' not found — auto-created with status inactive.",
            employee_id,
        )
        return {
            "employee_id": employee_id,
            "vpn_access": False,
            "status": "not_found",
            "reactivated": False,
            "message": (
                f"No VPN record found for employee '{employee_id}'. "
                "A new record has been created with status inactive "
                "and flagged for IT provisioning."
            ),
        }

    data = doc.to_dict()
    current_status: str = data.get("status", "").lower()
    reactivated = False

    if current_status == "inactive":
        doc_ref.update({"status": "active", "updated_at": now_utc()})
        current_status = "active"
        reactivated = True
        logger.info(
            "VPN access: employee '%s' was inactive — reactivated to active.", employee_id
        )
    else:
        logger.info(
            "VPN access: employee '%s' status='%s'.", employee_id, current_status
        )

    vpn_access = current_status == "active"
    return {
        "employee_id": employee_id,
        "vpn_access": vpn_access,
        "status": current_status,
        "reactivated": reactivated,
        "message": (
            f"Employee '{employee_id}' was reactivated and now has VPN access."
            if reactivated
            else f"Employee '{employee_id}' VPN status: {current_status}."
        ),
    }


def _update_ticket(
    ticket_id: str,
    status: str,
    description: str | None = None,
    note: str | None = None,
    employee_id: str | None = None,
) -> dict:
    """
    Create or update a support ticket in Firestore.
    employee_id is stored as foreign key linking to vpn_user_access.
    Raises ValueError on bad input.
    """
    ticket_id = str(ticket_id).strip()
    if not ticket_id:
        raise ValueError("'ticket_id' must be a non-empty string.")
    if not status or status not in VALID_TICKET_STATUSES:
        raise ValueError(
            f"'status' must be one of {sorted(VALID_TICKET_STATUSES)}. Got: '{status}'."
        )

    db = get_db()
    doc_ref = db.collection(COLLECTION_SUPPORT_TICKET).document(ticket_id)
    doc = doc_ref.get()
    ts_now = now_utc()

    if doc.exists:
        update_payload: dict[str, Any] = {"status": status, "updated_at": ts_now}
        if description is not None:
            update_payload["description"] = description
        if note is not None:
            update_payload["note"] = note
        if employee_id is not None:
            update_payload["employee_id"] = employee_id
        doc_ref.update(update_payload)
        action = "updated"
        logger.info("Ticket '%s' updated -> status='%s'.", ticket_id, status)
    else:
        create_payload: dict[str, Any] = {
            "status": status,
            "description": description or "",
            "reported_time": ts_now,
            "created_at": ts_now,
            "updated_at": ts_now,
            "employee_id": employee_id or "",
        }
        if note is not None:
            create_payload["note"] = note
        doc_ref.set(create_payload)
        action = "created"
        logger.info(
            "Ticket '%s' created with status='%s', employee_id='%s'.",
            ticket_id, status, employee_id or "not provided",
        )

    return {
        "ticket_id": ticket_id,
        "action": action,
        "status": status,
        "updated_at": ts_now,
        "message": f"Ticket '{ticket_id}' successfully {action} with status '{status}'.",
    }



def _raise_support_case(
    employee_id: str,
    employee_name: str,
    email: str,
    issue_type: str,
    description: str,
    ticket_id: str,
) -> dict:
    """
    Combined operation:
    1. Creates a support ticket in vpn_support_ticket
    2. Looks up employee in vpn_user_access by employee_id
    3. If found and inactive  -> reactivates to active
    4. If not found           -> auto-creates with status inactive
    5. Returns full result including previous/current status
       and reactivation details for the agent to narrate.
    """
    ts_now = now_utc()
    db = get_db()

    # ── Step 1: Create the support ticket ────────────────────────────────
    ticket_ref = db.collection(COLLECTION_SUPPORT_TICKET).document(ticket_id)
    ticket_ref.set({
        "ticket_id": ticket_id,
        "employee_id": employee_id,
        "employee_name": employee_name,
        "email": email,
        "issue_type": issue_type,
        "description": description,
        "status": "open",
        "reported_time": ts_now,
        "created_at": ts_now,
        "updated_at": ts_now,
    })
    logger.info("Support case '%s' created for employee '%s'.", ticket_id, employee_id)

    # ── Step 2: Look up employee in vpn_user_access ───────────────────────
    user_ref = db.collection(COLLECTION_VPN_ACCESS).document(employee_id)
    user_doc = user_ref.get()

    previous_status = None
    current_status = None
    reactivated = False
    user_action = None

    if user_doc.exists:
        data = user_doc.to_dict()
        previous_status = data.get("status", "unknown")

        if previous_status.lower() == "inactive":
            # Reactivate the user
            user_ref.update({
                "status": "active",
                "updated_at": ts_now,
                "last_reactivated_by": f"ticket:{ticket_id}",
            })
            current_status = "active"
            reactivated = True
            # Update ticket note with reactivation info
            ticket_ref.update({
                "note": f"Employee was inactive. Auto-reactivated via ticket {ticket_id}.",
                "updated_at": ts_now,
            })
            logger.info(
                "Employee '%s' reactivated from inactive to active via ticket '%s'.",
                employee_id, ticket_id,
            )
        else:
            current_status = previous_status
            logger.info(
                "Employee '%s' already has status '%s'. No change needed.",
                employee_id, current_status,
            )
        user_action = "reactivated" if reactivated else "found_active"
    else:
        # New user — never existed before.
        # Create directly as active so they can connect immediately.
        user_ref.set({
            "employee_id": employee_id,
            "employee_name": employee_name,
            "email": email,
            "status": "active",
            "created_at": ts_now,
            "updated_at": ts_now,
            "note": f"New user registered via ticket {ticket_id}.",
        })
        previous_status = "not_found"
        current_status = "active"
        reactivated = False
        user_action = "new_user"
        ticket_ref.update({
            "note": f"New user. Registered and activated via ticket {ticket_id}.",
            "updated_at": ts_now,
        })
        logger.info(
            "New employee '%s' registered as active via ticket '%s'.",
            employee_id, ticket_id,
        )

    # ── Step 3: Build a human-readable status message ─────────────────────
    # Three distinct cases with clear, accurate messages:
    if user_action == "new_user":
        # First time — never had a VPN account
        status_message = (
            f"You are a new user. "
            f"We have registered {employee_id} in the VPN system "
            f"and your account is now active. "
            f"Please try connecting now."
        )
    elif user_action == "reactivated":
        # Had an account but it was inactive — now reactivated
        status_message = (
            f"Your account {employee_id} was previously inactive. "
            f"We have reactivated it back to active. "
            f"Please try connecting now."
        )
    else:
        # Already active — no change needed
        status_message = (
            f"Your account {employee_id} is already active. "
            f"Please try connecting now."
        )

    return {
        "ticket_id": ticket_id,
        "ticket_status": "open",
        "employee_id": employee_id,
        "employee_name": employee_name,
        "email": email,
        "previous_vpn_status": previous_status,
        "current_vpn_status": current_status,
        "reactivated": reactivated,
        "user_action": user_action,
        "status_message": status_message,
        "message": (
            f"Support case {ticket_id} created. "
            f"VPN status: was {previous_status}, now {current_status}. "
            f"Reactivated: {reactivated}."
        ),
    }

# ===========================================================================
# Flask routes — each function gets its own dedicated path
# ===========================================================================

@app.route("/health", methods=["GET"])
def health_check():
    """Lightweight liveness probe for Cloud Run."""
    return jsonify({"status": "healthy", "service": "vpn_management_tool"}), 200


@app.route("/check_vpn_user_access", methods=["POST"])
def route_check_vpn_user_access():
    """
    Check (and auto-reactivate if inactive) an employee's VPN access.

    Vertex AI sends flat JSON:
        {"employee_id": "E001"}
    """
    body, err = get_json_body()
    if err:
        return jsonify(err[0]), err[1]

    employee_id = body.get("employee_id", "").strip()
    if not employee_id:
        logger.error("check_vpn_user_access: missing employee_id")
        resp, status = error_response(
            "Parameter 'employee_id' is required.", "VALIDATION_ERROR"
        )
        return jsonify(resp), status

    try:
        result = _check_vpn_user_access(employee_id)
        return jsonify(success_response(result)), 200
    except ValueError as exc:
        logger.error("Validation error: %s", exc)
        resp, status = error_response(str(exc), "VALIDATION_ERROR")
        return jsonify(resp), status
    except Exception as exc:
        logger.exception("Unexpected error in check_vpn_user_access.")
        resp, status = error_response(f"Internal error: {exc}", "INTERNAL_ERROR")
        return jsonify(resp), status


@app.route("/update_ticket", methods=["POST"])
def route_update_ticket():
    """
    Create or update a VPN support ticket.

    Vertex AI sends flat JSON:
        {
            "ticket_id": "T-001",
            "status": "open",
            "description": "...",   (optional)
            "note": "..."           (optional)
        }
    """
    body, err = get_json_body()
    if err:
        return jsonify(err[0]), err[1]

    ticket_id = body.get("ticket_id", "").strip()
    status = body.get("status", "").strip()

    if not ticket_id:
        resp, http = error_response("Parameter 'ticket_id' is required.", "VALIDATION_ERROR")
        return jsonify(resp), http
    if not status:
        resp, http = error_response("Parameter 'status' is required.", "VALIDATION_ERROR")
        return jsonify(resp), http

    try:
        result = _update_ticket(
            ticket_id=ticket_id,
            status=status,
            description=body.get("description"),
            note=body.get("note"),
            employee_id=body.get("employee_id"),
        )
        return jsonify(success_response(result)), 200
    except ValueError as exc:
        logger.error("Validation error: %s", exc)
        resp, http = error_response(str(exc), "VALIDATION_ERROR")
        return jsonify(resp), http
    except Exception as exc:
        logger.exception("Unexpected error in update_ticket.")
        resp, http = error_response(f"Internal error: {exc}", "INTERNAL_ERROR")
        return jsonify(resp), http



@app.route("/raise_support_case", methods=["POST"])
def route_raise_support_case():
    """
    Combined endpoint: creates ticket + checks/reactivates VPN user in one call.

    Vertex AI sends flat JSON:
        {
            "employee_id":   "qrs-34",
            "employee_name": "Randy",
            "email":         "randy@acme.com",
            "issue_type":    "VPN Connection",
            "description":   "Cannot connect from home",
            "ticket_id":     "T-A1B2C3"
        }
    """
    body, err = get_json_body()
    if err:
        return jsonify(err[0]), err[1]

    employee_id   = body.get("employee_id",   "").strip()
    employee_name = body.get("employee_name", "").strip()
    email         = body.get("email",         "").strip()
    issue_type    = body.get("issue_type",    "").strip()
    description   = body.get("description",   "").strip()
    ticket_id     = body.get("ticket_id",     "").strip()

    missing = [f for f, v in [
        ("employee_id", employee_id),
        ("employee_name", employee_name),
        ("email", email),
        ("issue_type", issue_type),
        ("ticket_id", ticket_id),
    ] if not v]

    if missing:
        resp, http = error_response(
            f"Missing required parameters: {', '.join(missing)}", "VALIDATION_ERROR"
        )
        return jsonify(resp), http

    try:
        result = _raise_support_case(
            employee_id=employee_id,
            employee_name=employee_name,
            email=email,
            issue_type=issue_type,
            description=description or issue_type,
            ticket_id=ticket_id,
        )
        return jsonify(success_response(result)), 200
    except ValueError as exc:
        logger.error("Validation error in raise_support_case: %s", exc)
        resp, http = error_response(str(exc), "VALIDATION_ERROR")
        return jsonify(resp), http
    except Exception as exc:
        logger.exception("Unexpected error in raise_support_case.")
        resp, http = error_response(f"Internal error: {exc}", "INTERNAL_ERROR")
        return jsonify(resp), http

# ===========================================================================
# Entry point (local dev only — Cloud Run uses gunicorn)
# ===========================================================================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port, debug=False)
