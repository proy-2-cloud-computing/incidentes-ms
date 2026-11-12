from .lib import repo
from .lib.auth import get_context
from .lib.dominio import serializar
from .lib.errores import HttpError
from .lib.http import envolver, id_de_path, query_params, respuesta


@envolver
def handler(event):
    """GET /incidentes/{id}[?historial=true]
    Un ID de otro tenant devuelve 404 (la busqueda siempre usa el tenant_id del token)."""
    ctx = get_context(event)
    incidente_id = id_de_path(event)
    item = repo.get_incidente(ctx.tenant_id, incidente_id)
    if not item:
        raise HttpError(404, "Incidente no encontrado")

    salida = serializar(item)
    if query_params(event).get("historial") == "true":
        salida["historial"] = repo.listar_historial(ctx.tenant_id, incidente_id)
    return respuesta(200, salida)
