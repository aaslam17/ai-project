"""Strict Pydantic v2 JSON schemas for OrderGuard tool inputs, outputs, and audits.

Enforces explicit JSON Schema validation (`extra="forbid"`, regex patterns, bounds,
and rich field descriptions) to constrain LLM tool arguments and validate outputs.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class StrictSchemaModel(BaseModel):
    """Base schema enforcing strict field validation with zero undeclared keys."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OrderItemSchema(StrictSchemaModel):
    """Line item belonging to a customer order."""

    sku: str = Field(
        ...,
        min_length=3,
        max_length=32,
        description="Unique product SKU identifier (e.g., 'SKU-MOUSE-01').",
    )
    name: str = Field(
        ...,
        min_length=2,
        max_length=120,
        description="Human-readable product title.",
    )
    category: Literal["electronics", "apparel", "home_office", "digital_gift_card"] = (
        Field(
            ...,
            description="Merchandise category used to determine return eligibility.",
        )
    )
    quantity: int = Field(
        ...,
        ge=1,
        le=100,
        description="Number of units purchased in this line item.",
    )
    unit_price_usd: float = Field(
        ...,
        gt=0.0,
        le=50000.0,
        description="Unit purchase price in USD.",
    )


class OrderLookupInput(StrictSchemaModel):
    """Validated input schema for `lookup_customer_order_details`."""

    order_id: str = Field(
        ...,
        pattern=r"^ORD-\d{4,8}$",
        description="Customer order identifier in 'ORD-XXXX' format (e.g., 'ORD-1001').",
    )


class OrderDetailsOutput(StrictSchemaModel):
    """Validated output schema returned by `lookup_customer_order_details`."""

    status: Literal["success", "error"] = Field(
        ..., description="Execution status of the lookup operation."
    )
    order_id: str = Field(..., description="Normalized order identifier.")
    customer_tier: Literal["standard", "gold", "enterprise"] | None = Field(
        default=None, description="Loyalty tier associated with the order."
    )
    fulfillment_status: (
        Literal["PROCESSING", "IN_TRANSIT", "DELIVERED", "RETURNED", "REFUNDED"] | None
    ) = Field(default=None, description="Current shipment/fulfillment lifecycle state.")
    order_date_iso: str | None = Field(
        default=None, description="ISO-8601 purchase date (YYYY-MM-DD)."
    )
    days_since_delivery: int | None = Field(
        default=None,
        ge=0,
        description="Elapsed days since delivery, or None if not yet delivered.",
    )
    total_amount_usd: float | None = Field(
        default=None, ge=0.0, description="Total order value in USD."
    )
    items: list[OrderItemSchema] = Field(
        default_factory=list, description="Line items included in the order."
    )
    error_code: str | None = Field(
        default=None, description="Machine-readable error code when status='error'."
    )
    error_message: str | None = Field(
        default=None, description="Human-readable explanation of what went wrong."
    )
    recovery_instruction: str | None = Field(
        default=None,
        description="Actionable instruction telling the LLM how to recover.",
    )


class RefundEligibilityInput(StrictSchemaModel):
    """Validated input schema for `evaluate_refund_policy_eligibility`."""

    order_id: str = Field(
        ...,
        pattern=r"^ORD-\d{4,8}$",
        description="Order identifier in 'ORD-XXXX' format (e.g., 'ORD-1001').",
    )
    reason: Literal[
        "defective_item",
        "damaged_in_shipping",
        "wrong_item_sent",
        "changed_mind",
        "late_delivery",
    ] = Field(
        ...,
        description="Normalized customer reason for requesting a refund.",
    )


class RefundEligibilityOutput(StrictSchemaModel):
    """Validated output schema returned by `evaluate_refund_policy_eligibility`."""

    status: Literal["success", "error"] = Field(
        ..., description="Execution status of the policy evaluation."
    )
    order_id: str = Field(..., description="Evaluated order identifier.")
    eligible: bool = Field(
        ..., description="True if the order qualifies for a refund under policy."
    )
    policy_code: str = Field(
        ...,
        description="Policy rule code applied (e.g., 'ELIGIBLE_STANDARD_30D', 'FINAL_SALE_DIGITAL').",
    )
    refundable_amount_usd: float = Field(
        ...,
        ge=0.0,
        description="Calculated refund amount in USD (0.0 when ineligible).",
    )
    requires_human_approval: bool = Field(
        ...,
        description="Whether human-in-the-loop confirmation is mandatory to disburse.",
    )
    policy_explanation: str = Field(
        ..., description="Detailed explanation of how return policy rules applied."
    )
    error_code: str | None = Field(default=None)
    error_message: str | None = Field(default=None)
    recovery_instruction: str | None = Field(default=None)


class IssueRefundInput(StrictSchemaModel):
    """Validated input schema for `issue_verified_customer_refund`."""

    order_id: str = Field(
        ...,
        pattern=r"^ORD-\d{4,8}$",
        description="Verified order identifier in 'ORD-XXXX' format.",
    )
    refund_amount_usd: float = Field(
        ...,
        gt=0.0,
        le=1000.0,
        description="Verified positive refund amount in USD (max $1,000.00).",
    )
    reason: Literal[
        "defective_item",
        "damaged_in_shipping",
        "wrong_item_sent",
        "changed_mind",
        "late_delivery",
    ] = Field(..., description="Verified reason code matching the policy audit.")


class RefundReceiptOutput(StrictSchemaModel):
    """Validated output schema returned by `issue_verified_customer_refund`."""

    status: Literal["success", "pending_confirmation", "error"] = Field(
        ..., description="Execution status of the refund disbursement."
    )
    order_id: str = Field(..., description="Order identifier associated with receipt.")
    refund_id: str | None = Field(
        default=None, description="Immutable refund transaction receipt ID."
    )
    refunded_amount_usd: float = Field(
        default=0.0, ge=0.0, description="Disbursed USD amount."
    )
    signature_digest: str | None = Field(
        default=None,
        description="HMAC audit digest proving authorized refund execution.",
    )
    message: str = Field(..., description="Customer-facing summary of the outcome.")
    error_code: str | None = Field(default=None)
    recovery_instruction: str | None = Field(default=None)


class EscalationTicketInput(StrictSchemaModel):
    """Validated input schema for `create_priority_escalation_ticket`."""

    order_id: str = Field(
        ...,
        pattern=r"^ORD-\d{4,8}$",
        description="Order identifier in 'ORD-XXXX' format.",
    )
    priority: Literal["P1", "P2", "P3"] = Field(
        ...,
        description="Ticket urgency: P1 (fraud/safety), P2 (damaged/high-value), P3 (policy review).",
    )
    issue_summary: str = Field(
        ...,
        min_length=10,
        max_length=500,
        description="PII-free summary of the customer issue for human specialists.",
    )


class EscalationTicketOutput(StrictSchemaModel):
    """Validated output schema returned by `create_priority_escalation_ticket`."""

    status: Literal["success", "error"] = Field(
        ..., description="Execution status of ticket creation."
    )
    ticket_id: str | None = Field(
        default=None, description="Generated support escalation ticket identifier."
    )
    order_id: str = Field(..., description="Associated order identifier.")
    priority: str = Field(..., description="Assigned priority level.")
    sla_hours: int = Field(
        default=24, ge=1, description="Target specialist response SLA in hours."
    )
    message: str = Field(..., description="Confirmation details for the customer.")
    error_code: str | None = Field(default=None)
    recovery_instruction: str | None = Field(default=None)


TOOL_JSON_SCHEMAS: dict[str, dict[str, dict]] = {
    "lookup_customer_order_details": {
        "input_schema": OrderLookupInput.model_json_schema(),
        "output_schema": OrderDetailsOutput.model_json_schema(),
    },
    "evaluate_refund_policy_eligibility": {
        "input_schema": RefundEligibilityInput.model_json_schema(),
        "output_schema": RefundEligibilityOutput.model_json_schema(),
    },
    "issue_verified_customer_refund": {
        "input_schema": IssueRefundInput.model_json_schema(),
        "output_schema": RefundReceiptOutput.model_json_schema(),
    },
    "create_priority_escalation_ticket": {
        "input_schema": EscalationTicketInput.model_json_schema(),
        "output_schema": EscalationTicketOutput.model_json_schema(),
    },
}


def format_validation_recovery(exc: ValidationError, tool_name: str) -> str:
    """Builds an actionable LLM recovery prompt from a Pydantic ValidationError."""
    details = "; ".join(
        f"field '{'.'.join(str(loc) for loc in err['loc'])}': {err['msg']}"
        for err in exc.errors()
    )
    return (
        f"Invalid arguments passed to `{tool_name}` ({details}). "
        "Verify parameter formats (for example, `order_id` must match 'ORD-1001' "
        "and `reason` must be an allowed enum value), ask the customer for any "
        f"missing details if needed, and retry `{tool_name}`."
    )
