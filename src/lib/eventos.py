import json
import logging
import os
import uuid

import boto3

from .dominio import ahora_iso

logger = logging.getLogger()
sns = boto3.client("sns")


def crear_evento(tipo, tenant_id, id_, estado, actor_id, datos=None):
    """Formato del contrato de eventos (seccion 5.3 de la guia)."""
    return {
        "evento_id": str(uuid.uuid4()),
        "tipo": tipo,
        "tenant_id": tenant_id,
        "id": id_,
        "estado": estado,
        "actor_id": actor_id,
        "fecha": ahora_iso(),
        "datos": datos or {},
    }


def publicar(eventos):
    """Publica en el topic incidentes-eventos. Si SNS falla el incidente ya esta guardado:
    se registra el error y no se rompe la respuesta al usuario."""
    for e in eventos:
        try:
            sns.publish(
                TopicArn=os.environ["TOPIC_ARN"],
                Message=json.dumps(e),
                MessageAttributes={
                    "tenant_id": {"DataType": "String", "StringValue": e["tenant_id"]},
                    "tipo": {"DataType": "String", "StringValue": e["tipo"]},
                },
            )
        except Exception as err:  # noqa: BLE001
            logger.error(json.dumps({"mensaje": "Fallo publicando evento", "tipo": e["tipo"], "id": e["id"], "error": str(err)}))
