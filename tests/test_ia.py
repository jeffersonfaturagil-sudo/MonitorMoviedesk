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
    body = json.dumps(payload).encode("utf-8")
    h = _Fake(path, body)
    h.do_POST()
    return h


def test_ia_sem_texto():
    h = _post("/api/ia", {})
    assert h.code == 400


def test_ia_sem_token(monkeypatch):
    monkeypatch.setattr(server, "OPENROUTER_API_KEY", "")
    h = _post("/api/ia", {"texto": "erro nfse"})
    assert h.code == 400


def test_ia_cache_hit(monkeypatch):
    import hashlib
    texto = "erro nfse teste cache"
    chave = hashlib.sha256((texto + "|").encode("utf-8")).hexdigest()
    cache = {chave: {"resposta": "resposta cacheada", "modelo": "m", "artigos": [], "macros": [], "intencao": "responder", "resposta_id": "r1", "fontes": []}}
    import pathlib
    p = pathlib.Path("cache/ia_cache.json")
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(server, "OPENROUTER_API_KEY", "fake")
    h = _post("/api/ia", {"texto": texto})
    assert h.code == 200
    j = h.json_body()
    assert j.get("cache") is True
    assert j.get("resposta") == "resposta cacheada"
    assert j.get("resposta_id") == "r1"


def test_ia_rastreabilidade_campos(monkeypatch):
    from unittest.mock import patch
    class Resp:
        status_code = 200
        def json(self):
            return {"choices": [{"message": {"content": "ok"}}]}
    def fake_post(*a, **k):
        return Resp()
    monkeypatch.setattr(server, "OPENROUTER_API_KEY", "fake")
    with patch("requests.post", fake_post):
        h = _post("/api/ia", {"texto": "erro nfse rastre"})
    assert h.code == 200
    j = h.json_body()
    assert "resposta_id" in j
    assert "fontes" in j
    assert "intencao" in j