"""Acceso a DynamoDB. La clave de ordenamiento de la tabla es `incidente_id` (INC#000123,
CONTADOR#INC, HIST#...). Toda consulta incluye tenant_id como partition key."""
import os

import boto3
from botocore.exceptions import ClientError

from .dominio import ESTADOS, ESTADOS_ABIERTOS, ahora_iso
from .errores import HttpError

_dynamodb = boto3.resource("dynamodb")
TABLE = os.environ.get("TABLE", "")
table = _dynamodb.Table(TABLE)
client = _dynamodb.meta.client  # acepta tipos nativos de Python tambien en transacciones


def _codigo(err):
    return err.response.get("Error", {}).get("Code")


def siguiente_numero(tenant_id):
    """ID consecutivo por tenant con UpdateItem ADD atomico (CONTADOR#INC)."""
    r = table.update_item(
        Key={"tenant_id": tenant_id, "incidente_id": "CONTADOR#INC"},
        UpdateExpression="ADD valor :uno",
        ExpressionAttributeValues={":uno": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(r["Attributes"]["valor"])


def get_incidente(tenant_id, incidente_id, consistente=False):
    """Siempre filtra por tenant_id: un ID de otro tenant simplemente no existe (404)."""
    r = table.get_item(Key={"tenant_id": tenant_id, "incidente_id": incidente_id}, ConsistentRead=consistente)
    return r.get("Item")


def guardar_nuevo(item, hist):
    client.transact_write_items(TransactItems=[
        {"Put": {"TableName": TABLE, "Item": item, "ConditionExpression": "attribute_not_exists(incidente_id)"}},
        {"Put": {"TableName": TABLE, "Item": hist}},
    ])


def guardar_cambios(tenant_id, incidente_id, cambios, estado_actual, hist):
    """Actualiza con control optimista (estado esperado) y escribe el historial en la misma transaccion."""
    nombres = {"#id": "incidente_id", "#estado": "estado"}
    valores = {":estadoActual": estado_actual}
    partes = []
    for i, (campo, valor) in enumerate(cambios.items()):
        nombres[f"#f{i}"] = campo
        valores[f":v{i}"] = valor
        partes.append(f"#f{i} = :v{i}")
    try:
        client.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": TABLE,
                "Key": {"tenant_id": tenant_id, "incidente_id": incidente_id},
                "UpdateExpression": "SET " + ", ".join(partes),
                "ConditionExpression": "attribute_exists(#id) AND #estado = :estadoActual",
                "ExpressionAttributeNames": nombres,
                "ExpressionAttributeValues": valores,
            }},
            {"Put": {"TableName": TABLE, "Item": hist}},
        ])
    except ClientError as err:
        if _codigo(err) == "TransactionCanceledException":
            raise HttpError(409, "El incidente fue modificado por otro usuario, vuelva a cargarlo e intente de nuevo") from err
        raise


def agregar_adjunto(tenant_id, incidente_id, key):
    try:
        table.update_item(
            Key={"tenant_id": tenant_id, "incidente_id": incidente_id},
            UpdateExpression="SET #adj = list_append(if_not_exists(#adj, :vacio), :nuevo)",
            ConditionExpression="attribute_exists(#id) AND (attribute_not_exists(#adj) OR NOT contains(#adj, :key))",
            ExpressionAttributeNames={"#adj": "adjuntos", "#id": "incidente_id"},
            ExpressionAttributeValues={":vacio": [], ":nuevo": [key], ":key": key},
        )
    except ClientError as err:
        if _codigo(err) != "ConditionalCheckFailedException":  # ya estaba registrado
            raise


def listar(tenant_id, estado, prioridad, limite, start_key):
    """Query paginado (nunca Scan). Con estado usa GSI1; sin estado recorre INC# de la tabla base."""
    usa_gsi = bool(estado)
    valores = {":t": tenant_id, ":p": f"{estado}#" if usa_gsi else "INC#"}
    base = {
        "KeyConditionExpression": "tenant_id = :t AND begins_with(estado_fecha, :p)" if usa_gsi
        else "tenant_id = :t AND begins_with(incidente_id, :p)",
        "ScanIndexForward": False,  # mas recientes primero
    }
    if usa_gsi:
        base["IndexName"] = "GSI1"
    if prioridad:
        base["FilterExpression"] = "#pr = :pr"
        base["ExpressionAttributeNames"] = {"#pr": "prioridad"}
        valores[":pr"] = prioridad
    base["ExpressionAttributeValues"] = valores

    items = []
    last_key = start_key
    vueltas = 0
    while True:
        kwargs = dict(base, Limit=limite - len(items))
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        r = table.query(**kwargs)
        items.extend(r.get("Items", []))
        last_key = r.get("LastEvaluatedKey")
        vueltas += 1
        if not last_key or len(items) >= limite or vueltas >= 25:
            break
    return items, last_key


def listar_historial(tenant_id, incidente_id):
    items = []
    last_key = None
    while True:
        kwargs = {
            "KeyConditionExpression": "tenant_id = :t AND begins_with(incidente_id, :p)",
            "ExpressionAttributeValues": {":t": tenant_id, ":p": f"HIST#{incidente_id}#"},
        }
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        r = table.query(**kwargs)
        items.extend(r.get("Items", []))
        last_key = r.get("LastEvaluatedKey")
        if not last_key:
            break
    return [{k: v for k, v in it.items() if k != "incidente_id"} for it in items]


def contar(tenant_id, estado, filtro=None):
    """Select COUNT sobre GSI1 (para el dashboard)."""
    total = 0
    last_key = None
    valores = {":t": tenant_id, ":e": f"{estado}#"}
    base = {
        "IndexName": "GSI1",
        "KeyConditionExpression": "tenant_id = :t AND begins_with(estado_fecha, :e)",
        "Select": "COUNT",
    }
    if filtro:
        base["FilterExpression"] = filtro["expr"]
        if filtro.get("names"):
            base["ExpressionAttributeNames"] = filtro["names"]
        valores.update(filtro["values"])
    base["ExpressionAttributeValues"] = valores
    while True:
        kwargs = dict(base)
        if last_key:
            kwargs["ExclusiveStartKey"] = last_key
        r = table.query(**kwargs)
        total += r["Count"]
        last_key = r.get("LastEvaluatedKey")
        if not last_key:
            return total


def resumen(tenant_id):
    f_critico = {"expr": "#pr = :p1", "names": {"#pr": "prioridad"}, "values": {":p1": "P1"}}
    f_vencido = {"expr": "sla_vence_en < :ahora", "values": {":ahora": ahora_iso()}}
    por_estado = [contar(tenant_id, e) for e in ESTADOS]
    criticos = sum(contar(tenant_id, e, f_critico) for e in ESTADOS_ABIERTOS)
    vencidos = sum(contar(tenant_id, e, f_vencido) for e in ESTADOS_ABIERTOS)
    return {
        "total": sum(por_estado),
        "por_estado": dict(zip(ESTADOS, por_estado)),
        "criticos_abiertos": criticos,
        "sla_vencidos": vencidos,
    }


def cerrar_incidente(tenant_id, incidente_id, evento):
    """Consumidor de problema-resuelto: cierra un incidente de forma idempotente.
    La clave del historial incluye evento_id + escritura condicional => un mensaje repetido no duplica nada."""
    actual = get_incidente(tenant_id, incidente_id, consistente=True)
    if not actual:
        return "no_existe"
    if actual["estado"] == "cerrado":
        return "ya_cerrado"
    ahora = ahora_iso()
    try:
        client.transact_write_items(TransactItems=[
            {"Update": {
                "TableName": TABLE,
                "Key": {"tenant_id": tenant_id, "incidente_id": incidente_id},
                "UpdateExpression": "SET #estado = :cerrado, estado_fecha = :ef, actualizado_en = :ahora",
                "ConditionExpression": "attribute_exists(#id) AND #estado = :estadoActual",
                "ExpressionAttributeNames": {"#estado": "estado", "#id": "incidente_id"},
                "ExpressionAttributeValues": {
                    ":cerrado": "cerrado",
                    ":ef": f"cerrado#{actual['creado_en']}",
                    ":ahora": ahora,
                    ":estadoActual": actual["estado"],
                },
            }},
            {"Put": {
                "TableName": TABLE,
                "Item": {
                    "tenant_id": tenant_id,
                    "incidente_id": f"HIST#{incidente_id}#{evento['fecha']}#{evento['evento_id']}",
                    "incidente_ref": incidente_id,
                    "evento": "cierre-por-problema",
                    "problema_id": evento.get("id"),
                    "evento_id": evento["evento_id"],
                    "estado_anterior": actual["estado"],
                    "estado_nuevo": "cerrado",
                    "actor_id": evento.get("actor_id"),
                    "fecha": ahora,
                },
                "ConditionExpression": "attribute_not_exists(incidente_id)",
            }},
        ])
        return "cerrado"
    except ClientError as err:
        razones = err.response.get("CancellationReasons") or []
        if _codigo(err) == "TransactionCanceledException" and len(razones) > 1 and razones[1].get("Code") == "ConditionalCheckFailed":
            return "duplicado"  # el evento ya se habia procesado
        raise  # cualquier otro fallo => SQS reintenta y al agotar los intentos va a la DLQ
