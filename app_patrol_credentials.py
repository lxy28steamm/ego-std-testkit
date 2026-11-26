"""Keep the optional LivFlow test account outside the repository."""
import json
import os
from pathlib import Path


def credential_path():
    return Path(os.environ['LOCALAPPDATA']) / 'EgoStdTestkit' / 'livflow-login.json'


def load_login():
    try:
        values = json.loads(credential_path().read_text(encoding='utf-8'))
    except FileNotFoundError:
        return {}
    if not isinstance(values, dict) or any(
            not isinstance(values.get(key, ''), str) for key in ('PHONE', 'PASSWORD', 'ROLE')):
        raise ValueError('本机测试账号格式无效')
    return {key: values.get(key, '') for key in ('PHONE', 'PASSWORD', 'ROLE')}


def accounts_path(scope):
    files = {'app': 'test-accounts.json', 'orangepi': 'orangepi-test-accounts.json'}
    return credential_path().with_name(files[scope])


def load_accounts(scope):
    path = accounts_path(scope)
    try:
        accounts = json.loads(path.read_text(encoding='utf-8'))
    except FileNotFoundError:
        if scope != 'app':
            return []
        legacy = load_login()
        if legacy.get('PHONE') and legacy.get('PASSWORD'):
            accounts = [{'id': 'legacy', 'name': '原有测试账号', **legacy}]
            save_accounts(scope, accounts)
            credential_path().unlink(missing_ok=True)
            return accounts
        return []
    if not isinstance(accounts, list) or any(
            not isinstance(account, dict) or any(
                not isinstance(account.get(key), str) for key in ('id', 'name', 'PHONE', 'PASSWORD', 'ROLE'))
            for account in accounts):
        raise ValueError('本机测试账号列表格式无效')
    return accounts


def save_accounts(scope, accounts):
    path = accounts_path(scope)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(accounts, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)
