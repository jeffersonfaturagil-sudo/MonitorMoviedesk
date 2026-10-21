import io
import json

import server


class _Fake(server.Handler):
    def __init__(self, path, body=b""):
        self.path = path
        self.headers = {"Content-Length": str(len(body))}
        self.rfile = io.BytesIO(body)
        self.wfile = io.BytesIO()
        self.code = None
        self._hdrs = []

    def send_response(self, code, message=None):
        self.code = code

    def send_header(self, key, value):
        self._hdrs.append((key, value))

    def end_headers(self):
        pass

    def log_message(self, *a):
        pass

    def json_body(self):
        return json.loads(self.wfile.getvalue().decode("utf-8"))


def _post(path, payload):
    import json as _j
    body = _j.dumps(payload).encode("utf-8")
    h = _Fake(path, body)
    h.do_POST()
    return h


def _get(path):
    h = _Fake(path)
    h.do_GET()
    return h


def test_casos_schema_get():
    h = _get("/api/ia/casos")
    assert h.code == 200
    j = h.json_body()
    assert "schema" in j
    assert "casos" in j
    assert isinstance(j["casos"], list)


def test_criar_caso_pendente():
    h = _post("/api/ia/casos", {"acao": "criar", "pergunta": "Erro NFS-e", "status": "pendente"})
    assert h.code == 200
    j = h.json_body()
    assert j["ok"] is True
    assert j["caso"]["status"] == "pendente"


def test_criar_caso_resolvido_exige_validacao():
    # sem validado_por/resposta_aprovada vira pendente
    h = _post("/api/ia/casos", {"acao": "criar", "pergunta": "Erro X", "status": "resolvido"})
    assert h.code == 200
    assert h.json_body()["caso"]["status"] == "pendente"


def test_aprovar_caso():
    # cria
    h = _post("/api/ia/casos", {"acao": "criar", "pergunta": "Caso Aprov", "status": "pendente"})
    cid = h.json_body()["caso"]["id"]
    h2 = _post("/api/ia/casos", {"acao": "aprovar", "id": cid, "validado_por": "analista", "resposta_aprovada": "fazer X"})
    assert h2.code == 200
    j = h2.json_body()
    assert j["caso"]["status"] == "resolvido"
    assert j["caso"]["validado_por"] == "analista"


def test_status_invalido_normaliza():
    h = _post("/api/ia/casos", {"acao": "criar", "pergunta": "Caso Y", "status": "foo"})
    assert h.code == 200
    assert h.json_body()["caso"]["status"] == "pendente"


def test_pergunta_obrigatoria():
    h = _post("/api/ia/casos", {"acao": "criar", "status": "pendente"})
    assert h.code == 400


def test_filtro_status():
    _post("/api/ia/casos", {"acao": "criar", "pergunta": "Caso Filtro", "status": "hipotese"})
    h = _get("/api/ia/casos?status=hipotese")
    assert h.code == 200
    assert all(c.get("status") == "hipotese" for c in h.json_body()["casos"])


def test_compatibilidade_caso_antigo():
    # caso sem campos novos nao quebra leitura
    import json, pathlib
    p = pathlib.Path("cache/ia_casos.json")
    raw = json.loads(p.read_text(encoding="utf-8"))
    raw["casos"].append({"pergunta": "antigo"})
    p.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    h = _get("/api/ia/casos")
    assert h.code == 200