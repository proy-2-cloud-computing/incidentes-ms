"""Pruebas de integracion de los handlers con AWS simulado (moto). Requiere: pip install -r requirements-dev.txt"""
import importlib
import json
import os
import unittest

try:
    from moto import mock_aws
except ImportError:  # pragma: no cover
    mock_aws = None

REGION = "us-east-1"


def evento(tenant, grupos="agente", body=None, id_=None, qs=None):
    ev = {"requestContext": {"authorizer": {"claims": {"custom:tenant_id": tenant, "cognito:groups": grupos, "sub": "u-1"}}}}
    if body is not None:
        ev["body"] = json.dumps(body) if not isinstance(body, str) else body
    if id_ is not None:
        ev["pathParameters"] = {"id": id_}
    if qs is not None:
        ev["queryStringParameters"] = qs
    return ev


def cuerpo(resp):
    return json.loads(resp["body"])


@unittest.skipUnless(mock_aws, "moto no instalado")
class HandlersTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.update(AWS_ACCESS_KEY_ID="x", AWS_SECRET_ACCESS_KEY="x", AWS_DEFAULT_REGION=REGION,
                          AWS_REGION=REGION, TABLE="itsm-incidentes-test", BUCKET="bucket-adjuntos-test")
        cls.mock = mock_aws()
        cls.mock.start()
        import boto3
        boto3.client("dynamodb").create_table(
            TableName="itsm-incidentes-test",
            BillingMode="PAY_PER_REQUEST",
            AttributeDefinitions=[{"AttributeName": a, "AttributeType": "S"} for a in ("tenant_id", "incidente_id", "estado_fecha")],
            KeySchema=[{"AttributeName": "tenant_id", "KeyType": "HASH"}, {"AttributeName": "incidente_id", "KeyType": "RANGE"}],
            GlobalSecondaryIndexes=[{
                "IndexName": "GSI1",
                "KeySchema": [{"AttributeName": "tenant_id", "KeyType": "HASH"}, {"AttributeName": "estado_fecha", "KeyType": "RANGE"}],
                "Projection": {"ProjectionType": "ALL"},
            }],
        )
        boto3.client("s3").create_bucket(Bucket="bucket-adjuntos-test")
        sns, sqs = boto3.client("sns"), boto3.client("sqs")
        topic = sns.create_topic(Name="incidentes-eventos")["TopicArn"]
        cls.cola = sqs.create_queue(QueueName="eventos-test")["QueueUrl"]
        arn_cola = sqs.get_queue_attributes(QueueUrl=cls.cola, AttributeNames=["QueueArn"])["Attributes"]["QueueArn"]
        sns.subscribe(TopicArn=topic, Protocol="sqs", Endpoint=arn_cola, Attributes={"RawMessageDelivery": "true"})
        os.environ["TOPIC_ARN"] = topic
        cls.sqs = sqs
        for m in ("crear", "listar", "obtener", "actualizar", "adjuntos", "problemaResuelto"):
            setattr(cls, m, staticmethod(importlib.import_module(f"src.{m}").handler))
        cls.repo = importlib.import_module("src.lib.repo")

    @classmethod
    def tearDownClass(cls):
        cls.mock.stop()

    def eventos_publicados(self):
        tipos = []
        while True:
            r = self.sqs.receive_message(QueueUrl=self.cola, MaxNumberOfMessages=10)
            msgs = r.get("Messages", [])
            if not msgs:
                return tipos
            for m in msgs:
                tipos.append(json.loads(m["Body"]))
                self.sqs.delete_message(QueueUrl=self.cola, ReceiptHandle=m["ReceiptHandle"])

    def nuevo(self, tenant, urgencia="media", impacto="medio", titulo="Falla de red en sede"):
        r = self.crear(evento(tenant, "solicitante", {"titulo": titulo, "urgencia": urgencia, "impacto": impacto}))
        self.assertEqual(r["statusCode"], 201, r["body"])
        return cuerpo(r)

    def test_crear_calcula_prioridad_y_publica_eventos(self):
        self.eventos_publicados()
        r = self.crear(evento("empresa-alfa", "solicitante",
                              {"titulo": "Caida del correo", "urgencia": "alta", "impacto": "alto", "tenant_id": "hack", "estado": "cerrado"}))
        self.assertEqual(r["statusCode"], 201)
        inc = cuerpo(r)
        self.assertEqual((inc["prioridad"], inc["estado"], inc["tenant_id"]), ("P1", "nuevo", "empresa-alfa"))
        self.assertTrue(inc["incidente_id"].startswith("INC#"))
        eventos = self.eventos_publicados()
        self.assertEqual(sorted(e["tipo"] for e in eventos), ["incidente-creado", "incidente-critico"])
        self.assertTrue(all(e["tenant_id"] == "empresa-alfa" and e["evento_id"] for e in eventos))

    def test_ids_consecutivos_por_tenant(self):
        a = self.nuevo("empresa-ids")["incidente_id"]
        b = self.nuevo("empresa-ids")["incidente_id"]
        self.assertEqual((a, b), ("INC#000001", "INC#000002"))

    def test_aislamiento_entre_tenants_devuelve_404(self):
        inc = self.nuevo("empresa-alfa")
        propio = self.obtener(evento("empresa-alfa", id_=inc["incidente_id"].replace("#", "%23")))
        ajeno = self.obtener(evento("empresa-beta", id_=inc["incidente_id"].replace("#", "%23")))
        self.assertEqual(propio["statusCode"], 200)
        self.assertEqual(ajeno["statusCode"], 404)

    def test_token_sin_tenant_es_rechazado(self):
        self.assertEqual(self.listar({"requestContext": {"authorizer": {"claims": {}}}})["statusCode"], 403)

    def test_flujo_de_estados_roles_y_resolucion(self):
        inc = self.nuevo("empresa-flujo")
        id_ = inc["incidente_id"]
        self.assertEqual(self.actualizar(evento("empresa-flujo", "solicitante", {"estado": "asignado"}, id_))["statusCode"], 403)
        r = self.actualizar(evento("empresa-flujo", "agente", {"asignado_a": "u-9"}, id_))
        self.assertEqual((r["statusCode"], cuerpo(r)["estado"]), (200, "asignado"))
        self.assertEqual(self.actualizar(evento("empresa-flujo", "agente", {"estado": "resuelto"}, id_))["statusCode"], 409)
        self.assertEqual(self.actualizar(evento("empresa-flujo", "agente", {"estado": "en_progreso"}, id_))["statusCode"], 200)
        self.assertEqual(self.actualizar(evento("empresa-flujo", "agente", {"estado": "resuelto"}, id_))["statusCode"], 400)
        self.eventos_publicados()
        r = self.actualizar(evento("empresa-flujo", "admin", {"estado": "resuelto", "solucion": "Reinicio del servicio"}, id_))
        self.assertEqual((r["statusCode"], cuerpo(r)["estado"]), (200, "resuelto"))
        self.assertIn("incidente-resuelto", [e["tipo"] for e in self.eventos_publicados()])
        det = cuerpo(self.obtener(evento("empresa-flujo", id_=id_, qs={"historial": "true"})))
        self.assertGreaterEqual(len(det["historial"]), 4)

    def test_cambiar_urgencia_recalcula_prioridad_y_escala(self):
        inc = self.nuevo("empresa-esc", "baja", "bajo")
        self.assertEqual(inc["prioridad"], "P4")
        self.eventos_publicados()
        r = self.actualizar(evento("empresa-esc", "agente", {"urgencia": "alta", "impacto": "alto"}, inc["incidente_id"]))
        self.assertEqual(cuerpo(r)["prioridad"], "P1")
        self.assertIn("incidente-critico", [e["tipo"] for e in self.eventos_publicados()])

    def test_listado_paginado_filtros_y_resumen(self):
        for i in range(5):
            self.nuevo("empresa-lista", "alta" if i < 2 else "baja", "alto" if i < 2 else "bajo")
        pag1 = cuerpo(self.listar(evento("empresa-lista", qs={"limit": "2"})))
        self.assertEqual(pag1["count"], 2)
        self.assertIsNotNone(pag1["next"])
        pag2 = cuerpo(self.listar(evento("empresa-lista", qs={"limit": "10", "next": pag1["next"]})))
        self.assertEqual(pag2["count"], 3)
        ids = [i["incidente_id"] for i in pag1["items"] + pag2["items"]]
        self.assertEqual(len(set(ids)), 5)
        p1 = cuerpo(self.listar(evento("empresa-lista", qs={"estado": "nuevo", "prioridad": "p1"})))
        self.assertEqual(p1["count"], 2)
        res = cuerpo(self.listar(evento("empresa-lista", qs={"resumen": "true"})))
        self.assertEqual((res["total"], res["por_estado"]["nuevo"], res["criticos_abiertos"]), (5, 5, 2))
        # el token de paginacion de otro tenant se rechaza
        self.assertEqual(self.listar(evento("otro-tenant", qs={"next": pag1["next"]}))["statusCode"], 400)

    def test_adjuntos_url_prefirmada_bajo_carpeta_del_tenant(self):
        inc = self.nuevo("empresa-adj")
        r = self.adjuntos(evento("empresa-adj", "solicitante", {"nombre_archivo": "../evidencia final.png", "content_type": "image/png"}, inc["incidente_id"]))
        self.assertEqual(r["statusCode"], 201, r["body"])
        data = cuerpo(r)
        self.assertEqual(data["key"], f"empresa-adj/incidentes/{inc['incidente_id'].replace('#', '-')}/evidencia_final.png")
        self.assertIn("bucket-adjuntos-test", data["upload_url"])
        det = cuerpo(self.obtener(evento("empresa-adj", id_=inc["incidente_id"])))
        self.assertEqual(det["adjuntos"], [data["key"]])
        self.assertEqual(self.adjuntos(evento("empresa-otro", "agente", {"nombre_archivo": "a.png"}, inc["incidente_id"]))["statusCode"], 404)

    def test_adjuntos_listar_con_url_de_descarga(self):
        inc = self.nuevo("empresa-desc")
        key = cuerpo(self.adjuntos(evento("empresa-desc", "agente", {"nombre_archivo": "log.txt"}, inc["incidente_id"])))["key"]
        ev = evento("empresa-desc", "solicitante", id_=inc["incidente_id"].replace("#", "%23"))
        ev["httpMethod"] = "GET"
        r = self.adjuntos(ev)
        self.assertEqual(r["statusCode"], 200, r["body"])
        data = cuerpo(r)
        self.assertEqual([a["nombre"] for a in data["adjuntos"]], ["log.txt"])
        self.assertEqual(data["adjuntos"][0]["key"], key)
        self.assertIn("bucket-adjuntos-test", data["adjuntos"][0]["download_url"])
        # otro tenant no ve el incidente
        ajeno = evento("empresa-otro", "agente", id_=inc["incidente_id"])
        ajeno["httpMethod"] = "GET"
        self.assertEqual(self.adjuntos(ajeno)["statusCode"], 404)

    def test_consumidor_problema_resuelto_idempotente_y_con_aislamiento(self):
        a = self.nuevo("empresa-cons")["incidente_id"]
        b = self.nuevo("empresa-cons")["incidente_id"]
        ajeno = self.nuevo("empresa-vecina")["incidente_id"]
        msg = {"evento_id": "e-1", "tipo": "problema-resuelto", "tenant_id": "empresa-cons", "id": "PRB#000045",
               "fecha": "2026-11-18T16:20:00.000Z", "datos": {"incidentes_relacionados": [a, b, ajeno, "INC#999999", "basura"]}}
        record = {"messageId": "m1", "body": json.dumps(msg)}
        self.assertEqual(self.problemaResuelto({"Records": [record]}), {"batchItemFailures": []})
        self.assertEqual(self.problemaResuelto({"Records": [record]}), {"batchItemFailures": []})  # entrega repetida
        estado = lambda t, i: cuerpo(self.obtener(evento(t, id_=i, qs={"historial": "true"})))  # noqa: E731
        self.assertEqual(estado("empresa-cons", a)["estado"], "cerrado")
        self.assertEqual(estado("empresa-cons", b)["estado"], "cerrado")
        self.assertEqual(estado("empresa-vecina", ajeno)["estado"], "nuevo")  # otro tenant intacto
        cierres = [h for h in estado("empresa-cons", a)["historial"] if h["evento"] == "cierre-por-problema"]
        self.assertEqual(len(cierres), 1)

    def test_consumidor_mensaje_invalido_va_a_reintento(self):
        r = self.problemaResuelto({"Records": [
            {"messageId": "malo-1", "body": "no-es-json"},
            {"messageId": "malo-2", "body": json.dumps({"tipo": "problema-resuelto"})},
        ]})
        self.assertEqual([f["itemIdentifier"] for f in r["batchItemFailures"]], ["malo-1", "malo-2"])


if __name__ == "__main__":
    unittest.main()
