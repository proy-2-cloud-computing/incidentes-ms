import os
import re

import boto3
from botocore.config import Config

from .lib import repo
from .lib.auth import get_context
from .lib.dominio import id_para_s3
from .lib.errores import HttpError
from .lib.http import envolver, id_de_path, parse_body, respuesta

s3 = boto3.client("s3", config=Config(signature_version="s3v4"))
EXPIRA_SEGUNDOS = 300


@envolver
def handler(event):
    """POST /incidentes/{id}/adjuntos  body: { nombre_archivo, content_type }
    Devuelve una URL prefirmada (PUT): el navegador sube directo a S3 bajo {tenant_id}/incidentes/{id}/"""
    ctx = get_context(event)
    incidente_id = id_de_path(event)
    body = parse_body(event)

    nombre_archivo = body.get("nombre_archivo")
    if not isinstance(nombre_archivo, str) or not nombre_archivo.strip():
        raise HttpError(400, "nombre_archivo es obligatorio")
    content_type = body.get("content_type")
    content_type = content_type.strip() if isinstance(content_type, str) and content_type.strip() else "application/octet-stream"
    nombre = re.sub(r"[^A-Za-z0-9._-]", "_", re.split(r"[\\/]", nombre_archivo)[-1])[-100:]
    if not nombre or set(nombre) == {"."}:
        raise HttpError(400, "nombre_archivo invalido")

    if not repo.get_incidente(ctx.tenant_id, incidente_id):
        raise HttpError(404, "Incidente no encontrado")

    key = f"{ctx.tenant_id}/incidentes/{id_para_s3(incidente_id)}/{nombre}"
    url = s3.generate_presigned_url(
        "put_object",
        Params={"Bucket": os.environ["BUCKET"], "Key": key, "ContentType": content_type},
        ExpiresIn=EXPIRA_SEGUNDOS,
        HttpMethod="PUT",
    )
    repo.agregar_adjunto(ctx.tenant_id, incidente_id, key)

    return respuesta(201, {
        "upload_url": url,
        "metodo": "PUT",
        "headers_requeridos": {"Content-Type": content_type},
        "key": key,
        "expira_en_segundos": EXPIRA_SEGUNDOS,
    })
