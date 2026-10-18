"""Validation errors (422) in pt-BR (#42).

Pydantic writes its own messages in English ("Field required", "String should have at
most 25 characters"). The handler keeps FastAPI's shape, ``{"detail": [{"loc", "msg",
"type"}]}``, which the app already reads, but with the message translated by ``type``
and without ``input``/``ctx``: Pydantic echoes the rejected value there, which may be
a password or a CPF.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from fastapi import Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response

from app.core.request_context import current_request_id

VALIDATION_ERROR = "validation_error"

# Field names the user would recognise, for messages that do not say which field.
_FIELDS = {
    "name": "Nome",
    "email": "E-mail",
    "cpf": "CPF",
    "phone": "Celular",
    "password": "Senha",
    "code": "Código",
    "title": "Título",
    "description": "Descrição",
    "category": "Categoria",
    "price": "Preço",
    "capacity": "Lotação",
    "event_date": "Início",
    "ends_at": "Fim",
    "message": "Mensagem",
}

_NUMBER = "Informe um número."
_DATE = "Data inválida."
_CHOICE = "Opção inválida."


def _ctx(error: Mapping[str, Any], key: str) -> Any:
    ctx = error.get("ctx")
    return ctx.get(key) if isinstance(ctx, Mapping) else None


_BY_TYPE: dict[str, Callable[[Mapping[str, Any]], str]] = {
    "missing": lambda e: "Campo obrigatório.",
    "string_too_short": lambda e: f"Use pelo menos {_ctx(e, 'min_length')} caracteres.",
    "string_too_long": lambda e: f"Use no máximo {_ctx(e, 'max_length')} caracteres.",
    "too_short": lambda e: f"Informe pelo menos {_ctx(e, 'min_length')} itens.",
    "too_long": lambda e: f"Informe no máximo {_ctx(e, 'max_length')} itens.",
    "string_type": lambda e: "Informe um texto.",
    "string_pattern_mismatch": lambda e: "Formato inválido.",
    "int_parsing": lambda e: _NUMBER,
    "int_type": lambda e: _NUMBER,
    "int_from_float": lambda e: "Informe um número inteiro.",
    "float_parsing": lambda e: _NUMBER,
    "float_type": lambda e: _NUMBER,
    "decimal_parsing": lambda e: _NUMBER,
    "decimal_type": lambda e: _NUMBER,
    "finite_number": lambda e: _NUMBER,
    "greater_than": lambda e: f"O valor precisa ser maior que {_ctx(e, 'gt')}.",
    "greater_than_equal": lambda e: f"O valor mínimo é {_ctx(e, 'ge')}.",
    "less_than": lambda e: f"O valor precisa ser menor que {_ctx(e, 'lt')}.",
    "less_than_equal": lambda e: f"O valor máximo é {_ctx(e, 'le')}.",
    "bool_parsing": lambda e: "Informe sim ou não.",
    "bool_type": lambda e: "Informe sim ou não.",
    "date_parsing": lambda e: _DATE,
    "date_type": lambda e: _DATE,
    "date_from_datetime_parsing": lambda e: _DATE,
    "datetime_parsing": lambda e: _DATE,
    "datetime_type": lambda e: _DATE,
    "datetime_from_date_parsing": lambda e: _DATE,
    "timezone_aware": lambda e: "Informe a data com o fuso horário.",
    "uuid_parsing": lambda e: "Identificador inválido.",
    "uuid_type": lambda e: "Identificador inválido.",
    "literal_error": lambda e: _CHOICE,
    "enum": lambda e: _CHOICE,
    "extra_forbidden": lambda e: "Campo não permitido.",
    "list_type": lambda e: "Informe uma lista.",
    "dict_type": lambda e: "Formato inválido.",
    "model_type": lambda e: "Formato inválido.",
    "model_attributes_type": lambda e: "Formato inválido.",
    "json_invalid": lambda e: "O corpo do pedido não é um JSON válido.",
    "url_parsing": lambda e: "Endereço inválido.",
    "url_type": lambda e: "Endereço inválido.",
}

# Our own validators already speak pt-BR; Pydantic only prefixes them.
_OWN_MESSAGE_TYPES = {"value_error", "assertion_error"}
_OWN_PREFIXES = ("Value error, ", "Assertion failed, ")


def _field_label(loc: Sequence[Any]) -> str | None:
    for part in reversed(loc):
        if isinstance(part, str):
            return _FIELDS.get(part)
    return None


def translate_error(error: Mapping[str, Any]) -> str:
    """One Pydantic error as a pt-BR sentence. Unknown types never leak English."""
    kind = str(error.get("type", ""))
    if kind in _OWN_MESSAGE_TYPES:
        message = str(error.get("msg", ""))
        for prefix in _OWN_PREFIXES:
            if message.startswith(prefix):
                message = message[len(prefix):]
        return message or "Valor inválido."

    loc = tuple(error.get("loc", ()))
    if kind == "missing" and loc == ("body",):
        return "Envie os dados do pedido."
    build = _BY_TYPE.get(kind)
    if build is None:
        return "Valor inválido."
    message = build(error)
    label = _field_label(loc) if kind not in {"json_invalid", "extra_forbidden"} else None
    if label is None:
        return message
    return f"{label}: {message[0].lower()}{message[1:]}"


async def validation_error_handler(request: Request, exc: Exception) -> Response:
    assert isinstance(exc, RequestValidationError)
    details = [
        {"loc": list(error.get("loc", ())), "msg": translate_error(error), "type": error.get("type")}
        for error in exc.errors()
    ]
    body = {"detail": details, "code": VALIDATION_ERROR, "request_id": current_request_id()}
    return JSONResponse(body, status_code=422)
