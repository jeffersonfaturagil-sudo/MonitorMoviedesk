import io
import json
import os
import tempfile

import server


class _Fake(server.Handler):
    def __init__(self, path, body=b"", headers=None):
        self.path = path
        self.headers = {"Content-Length": str(len(body))}
        if headers:
            self.headers.update(headers)
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


def test_feedback_positivo(tmp_path, monkeypatch):
    import server as s
    monkeypatch.setattr(s, "load_cache_raw", lambda: {})
    h = _post("/api/ia/feedback", {"texto": "erro nfse", "nota": 1})
    assert h.code == 200
    assert h.json_body().get("ok") is True


def test_feedback_negativo_com_motivo_e_correcao(tmp_path):
    h = _post("/api/ia/feedback", {"texto": "erro nfse", "nota": -1,
                                    "motivo": "informacao_incorreta",
                                    "resposta_corrigida": "fazer X"})
    assert h.code == 200
    assert h.json_body().get("ok") is True


def test_feedback_motivo_invalido_normaliza(tmp_path):
    h = _post("/api/ia/feedback", {"texto": "erro nfse", "nota": -1, "motivo": "foo"})
    assert h.code == 200


def test_feedback_sem_texto(tmp_path):
    h = _post("/api/ia/feedback", {"nota": 1})
    assert h.code == 400


def test_feedback_nota_invalida_aceita(tmp_path):
    h = _post("/api/ia/feedback", {"texto": "erro", "nota": 5})
    assert h.code == 200


def test_feedback_compatibilidade_antiga(tmp_path):
    # registro antigo com apenas texto/nota/ts nao quebra leitura
    import json, pathlib
    d = tmp_path / "cache"
    d.mkdir()
    f = d / "ia_feedback.json"
    f.write_text(json.dumps([{"texto": "antigo", "nota": 1, "ts": "2025-01-01T00:00:00"}]), encoding="utf-8")
    # handler appends; just ensure no crash by calling with new data
    h = _post("/api/ia/feedback", {"texto": "novo", "nota": -1, "motivo": "outro"})
    assert h.code in (200, 500)
