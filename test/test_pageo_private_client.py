import asyncio
import httpx
import pytest
from fastapi import HTTPException
import pageo_client


def test_private_proxy_does_not_follow_redirect_or_accept_arbitrary_paths(monkeypatch):
    monkeypatch.setenv('H5_WORKBENCH_ENABLED','1')
    monkeypatch.setenv('H5_PAGEO_URL','http://127.0.0.1:18806')
    monkeypatch.setenv('H5_PAGEO_SERVICE_KEY','test-machine-credential')
    observed=[]
    def reply(request):
        observed.append(request)
        return httpx.Response(302,headers={'Location':'https://example.invalid/not-allowed'})
    native=httpx.AsyncClient
    monkeypatch.setattr(pageo_client.httpx,'AsyncClient',lambda **opts:native(**opts,transport=httpx.MockTransport(reply)))
    async def run():
        async with pageo_client.PageoClient() as client:
            with pytest.raises(ValueError):await client.request('GET','/auth/api-keys')
            with pytest.raises(HTTPException):await client.request('GET','/api/h5/themes')
    asyncio.run(run())
    assert len(observed)==1
    assert observed[0].headers['X-API-Key']=='test-machine-credential'
    assert 'Authorization' not in observed[0].headers


def test_bad_service_configuration_fails_without_returning_credentials(monkeypatch):
    monkeypatch.setenv('H5_WORKBENCH_ENABLED','1')
    monkeypatch.setenv('H5_PAGEO_SERVICE_KEY','private-test-value')
    for address in ('http://localhost:bad','http://user:password@localhost','http://localhost?token=private-test-value'):
        monkeypatch.setenv('H5_PAGEO_URL',address)
        with pytest.raises(HTTPException) as rejected:pageo_client.config()
        assert 'private-test-value' not in str(rejected.value.detail)
