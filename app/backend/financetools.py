# app/backend/financetools.py
from typing import Any, Dict
from dataclasses import asdict

from rtmt import Tool, ToolResult, ToolResultDirection, RTMiddleTier

# --- Helper: compute rates from allowed terms ---
_ALLOWED_RATES = {
    6: 19.0,
    12: 21.0,
    24: 23.0,
    36: 25.0,
}

def _resolve_rate(period_months: int, interest_rate: float | None) -> float:
    # If the model/user didn’t pass a rate, infer from the term.
    if interest_rate is None:
        if period_months not in _ALLOWED_RATES:
            raise ValueError("Unsupported term. Allowed terms: 6, 12, 24, 36.")
        return _ALLOWED_RATES[period_months]
    return float(interest_rate)

def _calc_monthly_payment(amount: float, annual_rate: float, period_months: int) -> float:
    # Java-like formula, safe for zero-rate
    if period_months <= 0:
        raise ValueError("period_months must be > 0")
    monthly_rate = (annual_rate / 100.0) / 12.0
    if monthly_rate == 0.0:
        return round(amount / period_months, 2)
    factor = (1.0 + monthly_rate) ** period_months
    payment = amount * factor * monthly_rate / (factor - 1.0)
    return round(payment, 2)

def _calc_total_debt(amount: float, annual_rate: float, period_months: int) -> float:
    # Equivalent to monthly_payment * period, but mirrors your Java version
    monthly_rate = (annual_rate / 100.0) / 12.0
    if period_months <= 0:
        raise ValueError("period_months must be > 0")
    if monthly_rate == 0.0:
        return round(amount, 2)
    factor = (1.0 + monthly_rate) ** (-period_months)
    total = (amount * monthly_rate / (1.0 - factor)) * period_months
    return round(total, 2)

# ---- Tool Schemas (JSON Schema) ----
_monthly_schema: Dict[str, Any] = {
    "type": "function",
    "name": "calculate_monthly_payment",
    "description": "Compute the annuity monthly payment for a loan.",
    "parameters": {
        "type": "object",
        "properties": {
            "amount": {
                "type": "number",
                "description": "Principal amount in AZN (e.g., 50000).",
                "minimum": 1
            },
            "period_months": {
                "type": "integer",
                "description": "Loan term in months. Allowed: 6, 12, 24, 36.",
                "enum": [6, 12, 24, 36]
            },
            "interest_rate": {
                "type": "number",
                "description": "Annual interest rate in percent. Optional; if omitted, infer from the allowed term mapping."
            }
        },
        "required": ["amount", "period_months"],
        "additionalProperties": False
    }
}

_total_schema: Dict[str, Any] = {
    "type": "function",
    "name": "calculate_total_debt",
    "description": "Compute the total repayment over the full term.",
    "parameters": {
        "type": "object",
        "properties": {
            "amount": {
                "type": "number",
                "description": "Principal amount in AZN (e.g., 50000).",
                "minimum": 1
            },
            "period_months": {
                "type": "integer",
                "description": "Loan term in months. Allowed: 6, 12, 24, 36.",
                "enum": [6, 12, 24, 36]
            },
            "interest_rate": {
                "type": "number",
                "description": "Annual interest rate in percent. Optional; if omitted, infer from the allowed term mapping."
            }
        },
        "required": ["amount", "period_months"],
        "additionalProperties": False
    }
}

# ---- Tool Handlers ----
async def _monthly_handler(args: Any) -> ToolResult:
    amount = float(args["amount"])
    period = int(args["period_months"])
    rate = _resolve_rate(period, args.get("interest_rate"))
    monthly = _calc_monthly_payment(amount, rate, period)

    # Return to the *server/model* so it can speak a concise answer.
    return ToolResult(
        {
            "amount": amount,
            "period_months": period,
            "interest_rate": rate,
            "monthly_payment": monthly,
            "currency": "AZN"
        },
        ToolResultDirection.TO_SERVER
    )

async def _total_handler(args: Any) -> ToolResult:
    amount = float(args["amount"])
    period = int(args["period_months"])
    rate = _resolve_rate(period, args.get("interest_rate"))
    total = _calc_total_debt(amount, rate, period)

    return ToolResult(
        {
            "amount": amount,
            "period_months": period,
            "interest_rate": rate,
            "total_debt": total,
            "currency": "AZN"
        },
        ToolResultDirection.TO_SERVER
    )

# ---- Attach function for app.py ----
def attach_finance_tools(rtmt: RTMiddleTier) -> None:
    """
    Registers the two finance tools on the realtime session.
    """
    rtmt.tools["calculate_monthly_payment"] = Tool(schema=_monthly_schema, target=_monthly_handler)
    rtmt.tools["calculate_total_debt"]      = Tool(schema=_total_schema,   target=_total_handler)

