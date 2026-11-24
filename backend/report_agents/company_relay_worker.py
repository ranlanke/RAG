"""One isolated HTTP request; stdout is a private parent/child JSON protocol."""
import json
import os
import sys
import time
from urllib.parse import urlsplit

import httpx


def request_once(config, client):
    start=time.monotonic();content=[];reasoning_chars=0;usage={};finish=None
    timing={'first_event_seconds':None,'first_content_seconds':None,'content_chars':0};retry_after=None;provider_error_code=None
    progress_path=config.get('progress_path');last_report=0
    def report(force=False):
        nonlocal last_report
        if not progress_path:return
        now=time.monotonic()
        if not force and now-last_report<2:return
        last_report=now
        # Only counters and timing cross this channel: no prompts, key, prose
        # or reasoning text. Atomic replacement avoids partially read JSON.
        value={**timing,'seconds':round(now-start,3),'reasoning_chars':reasoning_chars}
        try:
            with open(progress_path+'.tmp','w',encoding='utf-8') as stream:json.dump(value,stream)
            os.replace(progress_path+'.tmp',progress_path)
        except OSError:pass
    def failure(category, *, status=None, received=False):
        timing.update(seconds=round(time.monotonic()-start,3),reasoning_chars=reasoning_chars)
        return {'ok':False,'category':category,'http_status':status,'response_received':received,
                'usage':usage,'timing':timing,'retry_after_seconds':retry_after,'provider_error_code':provider_error_code}
    try:
        with client.stream('POST',config['base_url'].rstrip('/')+'/chat/completions',
                headers={'Authorization':'Bearer '+os.environ['ARK_API_KEY']},json=config['payload']) as response:
            timing['headers_seconds']=round(time.monotonic()-start,3)
            report(True)
            hint=response.headers.get('retry-after')
            if hint:
                try:retry_after=max(0,float(hint))
                except ValueError:
                    from datetime import datetime,timezone
                    from email.utils import parsedate_to_datetime
                    try:retry_after=max(0,(parsedate_to_datetime(hint)-datetime.now(timezone.utc)).total_seconds())
                    except (ValueError,TypeError,OverflowError):pass
            if response.status_code!=200:
                # Keep only a numeric official provider code, never its body or message.
                if urlsplit(config['base_url']).hostname=='open.bigmodel.cn':
                    try:
                        response.read();error=response.json().get('error',{});code=str(error.get('code',''))
                        if code.isascii() and code.isdigit() and len(code)<=8:provider_error_code=code
                    except (ValueError,AttributeError,TypeError):pass
                return failure('http_error',status=response.status_code,received=True)
            for line in response.iter_lines():
                if time.monotonic()-start>config['deadline_seconds']:return failure('timeout',received=True)
                if not line.startswith('data:'):continue
                data=line[5:].strip()
                if data=='[DONE]':break
                if not data:continue
                timing['first_event_seconds']=timing['first_event_seconds'] or round(time.monotonic()-start,3)
                obj=json.loads(data)
                if obj.get('error'):
                    err=obj['error'];msg=str(err.get('message','') if isinstance(err,dict) else '').lower()
                    kind='concurrency_limit' if 'concurren' in msg else 'connection'
                    return failure(kind,received=True)
                if obj.get('usage'):usage=obj['usage']
                for choice in obj.get('choices') or []:
                    delta=choice.get('delta') or {}
                    text=delta.get('content') or ''
                    first_token=not reasoning_chars and not timing['content_chars']
                    reasoning_chars+=len(delta.get('reasoning_content') or delta.get('reasoning') or '')
                    if text:
                        timing['first_content_seconds']=timing['first_content_seconds'] or round(time.monotonic()-start,3)
                        content.append(text);timing['content_chars']+=len(text)
                    if choice.get('finish_reason'):finish=choice['finish_reason']
                    report(first_token or bool(text and timing['content_chars']==len(text)))
            if finish=='length':return failure('output_truncated',received=True)
            if finish!='stop' or not content:return failure('output_incomplete',received=True)
            timing.update(seconds=round(time.monotonic()-start,3),reasoning_chars=reasoning_chars)
            return {'ok':True,'content':''.join(content),'finish_reason':finish,'usage':usage,'timing':timing}
    except httpx.TimeoutException:return failure('timeout')
    except (httpx.TransportError,OSError):return failure('connection')
    except (ValueError,TypeError,KeyError):return failure('output_incomplete')


def main():
    sys.stdin.reconfigure(encoding='utf-8');sys.stdout.reconfigure(encoding='utf-8')
    config=json.load(sys.stdin)
    with httpx.Client(timeout=httpx.Timeout(120,connect=15,write=30,pool=15),http2=False) as client:
        result=request_once(config,client)
    print(json.dumps(result,ensure_ascii=False),flush=True)


if __name__=='__main__':main()
