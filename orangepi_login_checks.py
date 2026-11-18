"""Explicit opt-in login checks. Credentials stay in memory, never returned."""
import secrets
import urllib.error
import json

def run(api,account,password):
    rows=[]
    def put(key,status,detail):rows.append(dict(key=key,status=status,detail=detail))
    if not account or not password:
        for key in ('login','login_reject'):put(key,'未测','未提供专用测试账号和密码，不执行登录变更')
        return rows
    before=api.call('/api/v2/cloud/login/session')
    identity=before.get('phone') or before.get('username')
    if before.get('authenticated') and (not identity or identity!=account):
        for key in ('login','login_reject'):put(key,'未测','已有其他账号登录，或无法确认账号一致；为保留现有登录态，未执行切换')
        return rows
    from umi_functional import BUSY
    if api.call('/api/v2/session').get('state') in BUSY:
        for key in ('login','login_reject'):put(key,'未测','设备正在采集，未改变账号登录状态')
        return rows
    good=False
    try:
        api.call('/api/v2/cloud/login',{'account':account,'password':password})
        after=api.call('/api/v2/cloud/login/session')
        good=after.get('authenticated') is True and bool(after.get('account_id'))
        put('login','通过' if good else '失败','正确凭据登录请求完成；已通过登录会话接口回读确认' if good else '未回读到有效登录会话')
        if not good:
            put('login_reject','未测','正向登录未通过，不继续反向测试');return rows
        try:
            api.call('/api/v2/cloud/login',{'account':account,'password':secrets.token_urlsafe(48)})
            put('login_reject','失败','随机错误密码被接口接受，请复核账号认证实现')
        except urllib.error.HTTPError as exc:
            code=''
            try:
                detail=json.loads(exc.read()).get('detail') or {}
                if isinstance(detail,dict):code=detail.get('code','')
            except Exception:pass
            rejected=exc.code==401 or code in ('INVALID_CREDENTIALS','LOGIN_FAILED','AUTHENTICATION_FAILED','LIVEGO_LOGIN_FAILED')
            put('login_reject','通过' if rejected else '待确认',f'错误密码请求 HTTP {exc.code}；错误码={code or "未提供"}；限流、服务异常不算认证拒绝通过')
    except Exception as exc:
        put('login','失败','正向登录未完成：'+type(exc).__name__)
        put('login_reject','未测','未继续错误密码测试')
    finally:
        if good:
            try:
                api.call('/api/v2/cloud/login',{'account':account,'password':password})
                if api.call('/api/v2/cloud/login/session').get('authenticated') is not True:raise RuntimeError('restore failed')
            except Exception:put('login','待确认','反向测试后无法确认正确账号已恢复，请在设备页面重新登录')
    return rows
