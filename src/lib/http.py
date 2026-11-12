import base64
import functools
import json
import logging
from decimal import Decimal
from urllib.parse import unquote

from .dominio import normalizar_id
from .errores import HttpError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

HEADERS = {
    "Content-Type": "application/json",
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Headers": "Content-Type,Authorization",
    "Access-Control-Allow-Methods": "OPTIONS,GET,POST,PUT",
}


def _json_default(valor):
    if isinstance(valor, Decimal):
        return int(valor) if valor == valor.to_integral_value() else float(valor)
    raise TypeError(f"No serializable: {type(valor)}")


def respuesta(status, body):
    return {
        "statusCode": status,
        "headers": HEADERS,
        "body": json.dumps(body, default=_json_default, ensure_ascii=False),
    }


def envolver(fn):
    """Captura errores y siempre responde con headers CORS (tambien en los errores)."""

    @functools.wraps(fn)
    def interno(event, context=None):
        try:
            return fn(event)
        except HttpError as err:
            cuerpo = {"error": err.mensaje}
            if err.detalles:
                cuerpo["detalles"] = err.detalles
            return respuesta(err.status, cuerpo)
        except Exception:  # noqa: BLE001
            logger.exception("Error no controlado")
            return respuesta(500, {"error": "Error interno del servidor"})

    return interno


def parse_body(event):
    raw = event.get("body")
    if not raw:
        return {}
    if event.get("isBase64Encoded"):
        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw)
    except (ValueError, TypeError):
        raise HttpError(400, "El body no es un JSON valido") from None
    if not isinstance(body, dict):
        raise HttpError(400, "El body debe ser un objeto JSON")
    return body


def query_params(event):
    return event.get("queryStringParameters") or {}


def id_de_path(event):
    crudo = unquote((event.get("pathParameters") or {}).get("id") or "")
    incidente_id = normalizar_id(crudo)
    if not incidente_id:
        raise HttpError(400, "Identificador de incidente invalido (use INC%23000123 o 123)")
    return incidente_id
