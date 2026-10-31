import base64
import json
import os
from pathlib import Path
import urllib.request

from app_updater import _HttpsOnly, _verify_signature, _no_links
from control_trust import MODULUS_B64, EXPONENT

STATUS_URL='https://hazzaa.space/hazzaa-control/v1/status'
LIMIT=16384


def verify_policy(envelope):
    if not isinstance(envelope,dict) or set(envelope)!={'payload','signature'}:
        raise ValueError('Invalid application policy.')
    data=base64.b64decode(envelope['payload'],validate=True)
    signature=base64.b64decode(envelope['signature'],validate=True)
    if not 0<len(data)<=LIMIT:
        raise ValueError('Application policy is too large.')
    _verify_signature(data,signature,MODULUS_B64,EXPONENT)
    policy=json.loads(data)
    if (not isinstance(policy,dict) or policy.get('schema')!=1 or policy.get('app')!='hazzaa-plugins-control'
            or type(policy.get('revision')) is not int or not 0<=policy['revision']<=2**53-1
            or type(policy.get('enabled')) is not bool or not isinstance(policy.get('message'),str)
            or len(policy['message'])>300):
        raise ValueError('Unsupported application policy.')
    return policy


def cache_path():
    return Path(os.environ.get('LOCALAPPDATA',str(Path.home()/'AppData/Local')))/'Hazzaa Plugins/Control/policy.json'


def cached_policy(path=None):
    path=Path(path) if path is not None else cache_path()
    try:
        _no_links(path)
        if path.stat().st_size>LIMIT:
            return None
        return verify_policy(json.loads(path.read_text(encoding='utf-8')))
    except (OSError,ValueError,TypeError,KeyError):
        return None


def refresh_policy(path=None):
    path=Path(path) if path is not None else cache_path()
    previous=cached_policy(path)
    request=urllib.request.Request(STATUS_URL,headers={'Cache-Control':'no-cache','User-Agent':'HazzaaPlugins-Control/1'})
    with urllib.request.build_opener(_HttpsOnly()).open(request,timeout=5) as response:
        raw=response.read(LIMIT+1)
    if len(raw)>LIMIT:
        raise ValueError('Application policy response is too large.')
    envelope=json.loads(raw)
    policy=verify_policy(envelope)
    if previous and policy['revision']<previous['revision']:
        raise ValueError('Older application policy rejected.')
    if previous and policy['revision']==previous['revision'] and policy!=previous:
        raise ValueError('Conflicting application policy rejected.')
    try:
        _no_links(path)
        path.parent.mkdir(parents=True,exist_ok=True)
        pending=path.with_suffix('.pending')
        _no_links(pending)
        pending.write_bytes(raw)
        pending.replace(path)
    except (OSError,ValueError):
        pass
    return policy
