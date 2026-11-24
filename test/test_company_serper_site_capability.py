from web_search import WebSearchProvider
from test_company_recall_live_repairs import response


def test_free_account_site_filter_failure_does_not_disable_working_open_search(monkeypatch):
    monkeypatch.setenv('SERPER_API_KEY','offline-site-capability')
    monkeypatch.setenv('SERPER_FETCH_ENABLED','false')
    sent=[]
    def post(_url,**kw):
        data=kw['json'];sent.append(data)
        return response(400,{'message':'Query pattern not allowed for free accounts.'}) if 'site:' in data['q'] else response(200,{'organic':[{'title':'原件','link':'https://example.test/a'}]})
    monkeypatch.setattr('web_search.requests.post',post)
    p=WebSearchProvider();query=p._query_with_sites('目标公司 股东',['sse.com.cn','cninfo.com.cn'])
    first=p._search_serper(query,30);second=p._search_serper(query,30)
    assert len(first)==len(second)==1 and len(sent)==3
    assert all(r['num']==30 for r in sent)
    assert first.audit['query_adjustment']=='unsupported_account_site_filter_broadened'
    assert not first.audit['domain_filter_applied'] and first.audit['requested_query']==query
    assert first.audit['effective_query']=='目标公司 股东'
