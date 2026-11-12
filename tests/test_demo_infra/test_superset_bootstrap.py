from demo_env.mock_superset import DATASETS
from demo_env.superset_bootstrap import database_payload, dataset_payloads


def test_database_payload_uses_read_only_user() -> None:
    p = database_payload(host="mysql", password="pw")
    assert p["database_name"] == "demo_itam"
    assert p["sqlalchemy_uri"] == "mysql+pymysql://reader:pw@mysql:3306/demo_itam"
    assert p["expose_in_sqllab"] is True


def test_dataset_payloads_cover_every_mock_dataset() -> None:
    payloads = dataset_payloads(database_id=7)
    assert [p["table_name"] for p in payloads] == [d.table_name for d in DATASETS]
    assert all(p["database"] == 7 and p["schema"] == "demo_itam" for p in payloads)


def test_client_sends_session_cookie_back() -> None:
    """Superset's CSRF check needs the session cookie from the csrf_token call."""
    import threading
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from demo_env.superset_bootstrap import _Client

    seen: list[str | None] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Set-Cookie", "session=abc; Path=/")
            self.end_headers()
            self.wfile.write(b'{"result": "tok"}')

        def do_POST(self) -> None:
            # Drain the body first, otherwise the client may see a connection reset.
            self.rfile.read(int(self.headers.get("Content-Length", 0)))
            seen.append(self.headers.get("Cookie"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        client = _Client(f"http://127.0.0.1:{server.server_port}")
        assert client.call("GET", "/csrf") == {"result": "tok"}
        client.call("POST", "/thing", {"a": 1})
    finally:
        server.shutdown()
        server.server_close()
    assert seen == ["session=abc"]
