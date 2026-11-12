import base64
import json

from .lib import repo
from .lib.auth import get_context
from .lib.dominio import ESTADOS, PRIORIDADES, serializar
from .lib.errores import HttpError
from .lib.http import envolver, query_params, respuesta


def _codificar(key):
    return base64.urlsafe_b64encode(json.dumps(key).encode()).decode().rstrip("=")


def _decodificar(token, tenant_id):
    try:
        key = json.loads(base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)))
        if not isinstance(key, dict) or key.get("tenant_id") != tenant_id:
            raise ValueError("tenant")
        return key
    except (ValueError, TypeError):
        raise HttpError(400, "Token de paginacion (next) invalido") from None


@envolver
def handler(event):
    """GET /incidentes?estado=&prioridad=&limit=&next=   (paginado, nunca Scan)
    GET /incidentes?resumen=true                      (contadores para el dashboard)"""
    ctx = get_context(event)
    qs = query_params(event)

    if qs.get("resumen") == "true":
        return respuesta(200, repo.resumen(ctx.tenant_id))

    estado = qs["estado"].lower() if qs.get("estado") else None
    prioridad = qs["prioridad"].upper() if qs.get("prioridad") else None
    if estado and estado not in ESTADOS:
        raise HttpError(400, "estado invalido", {"permitidos": list(ESTADOS)})
    if prioridad and prioridad not in PRIORIDADES:
        raise HttpError(400, "prioridad invalida", {"permitidos": list(PRIORIDADES)})
    try:
        limite = min(max(int(qs.get("limit", 25)), 1), 100)
    except ValueError:
        limite = 25
    start_key = _decodificar(qs["next"], ctx.tenant_id) if qs.get("next") else None

    items, last_key = repo.listar(ctx.tenant_id, estado, prioridad, limite, start_key)
    return respuesta(200, {
        "items": [serializar(i) for i in items],
        "count": len(items),
        "next": _codificar(last_key) if last_key else None,
    })
