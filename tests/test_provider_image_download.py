"""Exercise the real render -> HTTP -> image decoding boundary with synthetic responses."""
import base64

import httpx
import pytest

from app.agent import providers
from app.agent.providers import Provider
from app.agent.store import AgentError


def render(monkeypatch, response, download=None):
    seen = []
    monkeypatch.setenv('AGENT_IMAGE_DOWNLOAD_HOSTS', 'images.example.test')
    def handler(request):
        seen.append(request)
        if request.method == 'POST':
            return httpx.Response(200, json=response)
        return download or httpx.Response(200, content=b'fixture-image-bytes')
    original = httpx.Client
    monkeypatch.setattr(providers.httpx, 'Client', lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    provider = Provider.__new__(Provider)
    provider.image_model = 'test-model'
    provider.image_size = '1024x1024'
    provider.image_url = 'https://api.example.test/v1'
    provider.image_key = 'synthetic-test-key'
    provider.require = lambda mode: None
    return provider, seen


def test_render_downloads_allowed_url_without_model_credentials(monkeypatch):
    provider, seen = render(monkeypatch, {'data': [{'url': 'https://images.example.test/result.png?expires=123'}]})
    assert provider.render({}, [], []) == b'fixture-image-bytes'
    assert len(seen) == 2
    assert seen[0].headers['authorization'] == 'Bearer synthetic-test-key'
    assert 'authorization' not in seen[1].headers


def test_render_still_accepts_base64(monkeypatch):
    provider, seen = render(monkeypatch, {'data': [{'b64_json': base64.b64encode(b'fixture-image-bytes').decode()}]})
    assert provider.render({}, [], []) == b'fixture-image-bytes'
    assert len(seen) == 1


@pytest.mark.parametrize('url', [
    'https://evil.example/result.png', 'http://images.example.test/result.png',
    'https://user:pass@images.example.test/result.png', 'https://images.example.test:8443/result.png',
    'https://images.example.test.evil.example/result.png', 'http://127.0.0.1/private',
])
def test_render_rejects_unapproved_urls_without_downloading(monkeypatch, url):
    provider, seen = render(monkeypatch, {'data': [{'url': url}]})
    with pytest.raises(AgentError) as error:
        provider.render({}, [], [])
    assert error.value.code == 'IMAGE_OUTPUT_INVALID'
    assert len(seen) == 1


def test_download_allowlist_defaults_to_empty(monkeypatch):
    provider, seen = render(monkeypatch, {'data': [{'url': 'https://images.example.test/a.png'}]})
    monkeypatch.delenv('AGENT_IMAGE_DOWNLOAD_HOSTS')
    with pytest.raises(AgentError):
        provider.render({}, [], [])
    assert len(seen) == 1


@pytest.mark.parametrize('response', [httpx.Response(302, headers={'location':'https://other.example/a.png'}), httpx.Response(200, content=b'x'*(10*1024*1024+1)), httpx.Response(200, content=b'')])
def test_download_rejects_redirects_oversized_and_empty_results(monkeypatch, response):
    provider, seen = render(monkeypatch, {'data': [{'url': 'https://images.example.test/a.png'}]}, response)
    with pytest.raises(AgentError) as error:
        provider.render({}, [], [])
    assert error.value.code == 'IMAGE_OUTPUT_INVALID'
    assert len(seen) == 2
