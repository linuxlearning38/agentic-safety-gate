def compose_response(plan) -> str:
    if getattr(plan, "mode", "") == "deterministic":
        return plan.answer
    raise ValueError(f"Unsupported response composition mode: {getattr(plan, 'mode', '')}")
