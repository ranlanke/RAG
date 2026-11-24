"""Presentation-only wrapper: isolate scripts and keep local anchors in the report."""
import re
from bs4 import BeautifulSoup

POLICY = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline' data:; "
          "img-src data:; font-src data:; media-src data:; connect-src 'none'; "
          "base-uri 'none'; form-action 'none'; frame-src 'none'; object-src 'none'")
ANCHORS = """<script>document.addEventListener('click',function(event){
var a=event.target.closest&&event.target.closest('a[href^="#"]');if(!a)return;
var id;try{id=decodeURIComponent(a.getAttribute('href').slice(1))}catch(e){return}
var node=id&&document.getElementById(id);if(!node)return;event.preventDefault();
for(var parent=node;parent;parent=parent.parentElement){if(parent.tagName==='DETAILS')parent.open=true}
node.scrollIntoView({block:'start'});
},true);</script>"""


def isolate_html(html):
    dom = BeautifulSoup(html, 'html.parser')
    for anchor in dom.select('a[href]'):
        if re.match(r'^/api/h5/reports/[^/]+/original/', anchor['href']):
            anchor['href'] = '#'
            anchor['onclick'] = 'return false'
            anchor['title'] = '原件请通过预览上方的“下载原件”入口查看'
    return ('<!doctype html><meta http-equiv="Content-Security-Policy" content="'+POLICY+'">'+ANCHORS+
            re.sub(r'<!doctype[^>]*>', '', str(dom), flags=re.I))
