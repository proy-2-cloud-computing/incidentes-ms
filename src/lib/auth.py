import re
from typing import NamedTuple

from .errores import HttpError

ROLES_ATENCION = ("agente", "admin")


class Contexto(NamedTuple):
    tenant_id: str
    user_id: str
    email: str
    grupos: list


def parse_grupos(raw):
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        return [str(g) for g in raw]
    return [g for g in re.split(r"[,\s]+", str(raw).strip("[]")) if g]


def get_context(event):
    """El tenant_id SIEMPRE sale de los claims del ID token validado por API Gateway."""
    claims = ((event.get("requestContext") or {}).get("authorizer") or {}).get("claims") or {}
    tenant_id = claims.get("custom:tenant_id")
    if not tenant_id or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", tenant_id):
        raise HttpError(403, "El token no contiene un tenant_id valido (use el ID token)")
    return Contexto(tenant_id, claims.get("sub"), claims.get("email"), parse_grupos(claims.get("cognito:groups")))


def requerir_rol(ctx, roles):
    if not any(g in roles for g in ctx.grupos):
        raise HttpError(403, "No tiene permisos para esta accion", {"roles_requeridos": list(roles)})
