from typing import Any, Dict, List
from decimal import Decimal, ROUND_HALF_UP

from rtmt import Tool, ToolResult, ToolResultDirection, RTMiddleTier

# --- Allowed terms and fixed annual rates (MUST match your prompt) ---
_ALLOWED_TERMS = [6, 12, 18, 24, 36]
_ALLOWED_RATES = {
    6: 21.0,
    12: 23.0,
    18: 24.0,
    24: 25.0,
    36: 27.0,
}

# Loan range constraints from the prompt
_MIN_AMOUNT = 1000.0
_MAX_AMOUNT = 10000.0


def _round_to_manat(x: float) -> float:
    """Round to nearest whole manat (no qəpik)."""
    return float(Decimal(str(x)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _resolve_rate(period_months: int, interest_rate: float | None) -> float:
    if interest_rate is None:
        if period_months not in _ALLOWED_RATES:
            raise ValueError(f"Unsupported term. Allowed terms: {', '.join(map(str, _ALLOWED_TERMS))}.")
        return _ALLOWED_RATES[period_months]
    return float(interest_rate)


def _calc_monthly_payment(amount: float, annual_rate: float, period_months: int) -> float:
    if period_months <= 0:
        raise ValueError("period_months must be > 0")
    monthly_rate = (annual_rate / 100.0) / 12.0
    if monthly_rate == 0.0:
        return _round_to_manat(amount / period_months)
    factor = (1.0 + monthly_rate) ** period_months
    payment = amount * factor * monthly_rate / (factor - 1.0)
    return _round_to_manat(payment)


def _calc_total_debt(amount: float, annual_rate: float, period_months: int) -> float:
    monthly_rate = (annual_rate / 100.0) / 12.0
    if period_months <= 0:
        raise ValueError("period_months must be > 0")
    if monthly_rate == 0.0:
        return _round_to_manat(amount)
    factor = (1.0 + monthly_rate) ** (-period_months)
    total = (amount * monthly_rate / (1.0 - factor)) * period_months
    return _round_to_manat(total)


def _calc_principal_from_monthly(monthly_limit: float, annual_rate: float, period_months: int) -> float:
    """Invert the annuity formula to get max principal for a monthly budget."""
    if period_months <= 0:
        raise ValueError("period_months must be > 0")
    monthly_rate = (annual_rate / 100.0) / 12.0
    if monthly_rate == 0.0:
        principal = monthly_limit * period_months
        return _round_to_manat(principal)
    factor = (1.0 + monthly_rate) ** period_months
    principal = monthly_limit * (factor - 1.0) / (monthly_rate * factor)
    return _round_to_manat(principal)


# ---- Tool Schemas ----
_monthly_schema: Dict[str, Any] = {
    "type": "function",
    "name": "calculate_monthly_payment",
    "description": "Compute the annuity monthly payment for a loan.",
    "parameters": {
        "type": "object",
        "properties": {
            "amount": {
                "type": "number",
                "description": "Principal amount in AZN.",
                "minimum": _MIN_AMOUNT,
                "maximum": _MAX_AMOUNT
            },
            "period_months": {
                "type": "integer",
                "description": "Loan term in months.",
                "enum": _ALLOWED_TERMS
            },
            "interest_rate": {
                "type": "number",
                "description": "Annual interest rate in percent. Optional; if omitted, infer from fixed mapping."
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
                "description": "Principal amount in AZN.",
                "minimum": _MIN_AMOUNT,
                "maximum": _MAX_AMOUNT
            },
            "period_months": {
                "type": "integer",
                "description": "Loan term in months.",
                "enum": _ALLOWED_TERMS
            },
            "interest_rate": {
                "type": "number",
                "description": "Annual interest rate in percent. Optional; if omitted, infer from fixed mapping."
            }
        },
        "required": ["amount", "period_months"],
        "additionalProperties": False
    }
}

_maxloan_schema: Dict[str, Any] = {
    "type": "function",
    "name": "calculate_max_loan_for_monthly_payment",
    "description": "Given a monthly payment limit, compute the maximum principal either for a specific term or for all allowed terms using fixed rate mapping.",
    "parameters": {
        "type": "object",
        "properties": {
            "monthly_limit": {
                "type": "number",
                "description": "Max monthly payment the customer can afford (AZN).",
                "minimum": 1
            },
            "period_months": {
                "type": "integer",
                "description": "Optional: if provided, compute only for this term.",
                "enum": _ALLOWED_TERMS
            },
            "interest_rate": {
                "type": "number",
                "description": "Optional annual interest rate in percent; if omitted, use fixed mapping."
            }
        },
        "required": ["monthly_limit"],
        "additionalProperties": False
    }
}


# ---- Tool Handlers ----
async def _monthly_handler(args: Any) -> ToolResult:
    amount = float(args["amount"])
    period = int(args["period_months"])
    rate = _resolve_rate(period, args.get("interest_rate"))
    monthly = _calc_monthly_payment(amount, rate, period)

    return ToolResult(
        {
            "amount": int(_round_to_manat(amount)),
            "period_months": period,
            "interest_rate": rate,
            "monthly_payment": int(monthly),
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
            "amount": int(_round_to_manat(amount)),
            "period_months": period,
            "interest_rate": rate,
            "total_debt": int(total),
            "currency": "AZN"
        },
        ToolResultDirection.TO_SERVER
    )


async def _maxloan_handler(args: Any) -> ToolResult:
    monthly_limit = float(args["monthly_limit"])
    period_opt = args.get("period_months")
    rate_opt = args.get("interest_rate")

    results: List[Dict[str, Any]] = []

    def _cap_amount(x: float) -> tuple[int, bool]:
        capped = False
        v = x
        if v > _MAX_AMOUNT:
            v = _MAX_AMOUNT
            capped = True
        return (int(_round_to_manat(v)), capped)

    if period_opt is not None:
        p = int(period_opt)
        r = _resolve_rate(p, rate_opt)
        principal = _calc_principal_from_monthly(monthly_limit, r, p)
        principal_capped, cap = _cap_amount(principal)
        results.append({
            "period_months": p,
            "interest_rate": r,
            "max_principal": principal_capped,
            "cap_applied": cap,
            "currency": "AZN",
        })
    else:
        for p in _ALLOWED_TERMS:
            r = _resolve_rate(p, rate_opt)
            principal = _calc_principal_from_monthly(monthly_limit, r, p)
            principal_capped, cap = _cap_amount(principal)
            results.append({
                "period_months": p,
                "interest_rate": r,
                "max_principal": principal_capped,
                "cap_applied": cap,
                "currency": "AZN",
            })

    return ToolResult({"results": results}, ToolResultDirection.TO_SERVER)


# ---- Attach for app.py ----
def attach_finance_tools(rtmt: RTMiddleTier) -> None:
    rtmt.tools["calculate_monthly_payment"] = Tool(schema=_monthly_schema, target=_monthly_handler)
    rtmt.tools["calculate_total_debt"] = Tool(schema=_total_schema, target=_total_handler)
    rtmt.tools["calculate_max_loan_for_monthly_payment"] = Tool(schema=_maxloan_schema, target=_maxloan_handler)
