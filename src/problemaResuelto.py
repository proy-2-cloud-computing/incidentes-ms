"""Consumidor SQS del evento "problema-resuelto" (topic problemas-eventos).
Cierra los incidentes listados en datos.incidentes_relacionados.
- Usa SIEMPRE el tenant_id del mensaje (nunca se asume).
- Idempotente: evento_id forma parte de la clave del historial (escritura condicional).
- Un mensaje invalido o que falla lanza error => SQS reintenta (maxReceiveCount del serverless.yml)
  y al agotar los intentos el mensaje pasa a la DLQ."""
import json
import logging
import re

from .lib import repo
from .lib.dominio import normalizar_id

logger = logging.getLogger()
logger.setLevel(logging.INFO)


def _leer_evento(record):
    crudo = json.loads(record["body"])  # JSON malformado => lanza error => DLQ
    # Soporta tambien suscripciones sin RawMessageDelivery (sobre SNS con "Message")
    evento = json.loads(crudo["Message"]) if isinstance(crudo, dict) and crudo.get("Type") == "Notification" and crudo.get("Message") else crudo
    ids = ((evento or {}).get("datos") or {}).get("incidentes_relacionados") if isinstance(evento, dict) else None
    if (
        not isinstance(evento, dict)
        or not isinstance(evento.get("evento_id"), str)
        or not isinstance(evento.get("fecha"), str)
        or not isinstance(evento.get("tenant_id"), str)
        or not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", evento["tenant_id"])
        or not isinstance(ids, list)
    ):
        raise ValueError("Mensaje problema-resuelto invalido")
    return evento, ids


def _procesar(record):
    evento, ids = _leer_evento(record)
    if evento.get("tipo") != "problema-resuelto":
        logger.warning(json.dumps({"mensaje": "Evento ignorado", "tipo": evento.get("tipo")}))
        return
    for id_crudo in ids:
        incidente_id = normalizar_id(id_crudo)
        if not incidente_id:
            logger.warning(json.dumps({"mensaje": "ID de incidente invalido, se omite", "id": str(id_crudo)}))
            continue
        resultado = repo.cerrar_incidente(evento["tenant_id"], incidente_id, evento)
        logger.info(json.dumps({
            "mensaje": "Incidente procesado", "tenant_id": evento["tenant_id"], "id": incidente_id,
            "problema": evento.get("id"), "resultado": resultado,
        }))


def handler(event, context=None):
    fallidos = []
    for record in event.get("Records", []):
        try:
            _procesar(record)
        except Exception as err:  # noqa: BLE001
            logger.error(json.dumps({"mensaje": "Fallo procesando mensaje", "messageId": record.get("messageId"), "error": str(err)}))
            fallidos.append({"itemIdentifier": record["messageId"]})
    return {"batchItemFailures": fallidos}
