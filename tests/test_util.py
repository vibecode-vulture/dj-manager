from djmanager import util


def test_ssl_context_works_without_system_certificates(monkeypatch):
    """Packaged builds on other systems: the system CA store may be missing entirely."""
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent/ca.pem")
    monkeypatch.setenv("SSL_CERT_DIR", "/nonexistent")
    monkeypatch.setattr(util, "_SSL", None)
    ctx = util.ssl_context()
    assert len(ctx.get_ca_certs()) > 50  # certifi's roots are loaded
    monkeypatch.setattr(util, "_SSL", None)
