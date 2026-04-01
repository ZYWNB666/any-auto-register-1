import sqlite3
import requests
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

DB = 'account_manager.db'
MAX_WORKERS = 32
LIST_PAGE_SIZE = 500
REQUEST_TIMEOUT = 20

cfg = dict(sqlite3.connect(DB).execute('SELECT key, value FROM configs').fetchall())
api = (cfg.get('freemail_api_url') or '').rstrip('/')
admin_token = cfg.get('freemail_admin_token') or ''
username = cfg.get('freemail_username') or ''
password = cfg.get('freemail_password') or ''

if not api:
    raise SystemExit('freemail_api_url 未配置，无法执行清理')

thread_local = threading.local()


def parse_json(resp):
    try:
        return resp.json()
    except Exception:
        return None


def parse_list_payload(data):
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ('items', 'data', 'list', 'mailboxes', 'users'):
            val = data.get(key)
            if isinstance(val, list):
                return val
    return []


def as_bool_deleted(data):
    if isinstance(data, dict):
        if 'deleted' in data:
            if 'success' in data:
                return bool(data.get('success')) and bool(data.get('deleted'))
            return bool(data.get('deleted'))
        if 'success' in data:
            return bool(data.get('success'))
    return False


def build_session():
    s = requests.Session()
    s.trust_env = False
    s.proxies = {}
    return s


def auth_session():
    s = build_session()

    if admin_token:
        s.headers.update({'Authorization': f'Bearer {admin_token}'})
        try:
            r = s.get(f'{api}/api/session', timeout=REQUEST_TIMEOUT)
            d = parse_json(r)
            if r.status_code == 200 and isinstance(d, dict) and d.get('authenticated'):
                return s, 'token'
        except Exception:
            pass

    s.headers.pop('Authorization', None)

    if not (username and password):
        raise SystemExit('freemail_admin_token 不可用且未配置 username/password，无法登录')

    lr = s.post(
        f'{api}/api/login',
        json={'username': username, 'password': password},
        timeout=REQUEST_TIMEOUT
    )
    ld = parse_json(lr)
    if lr.status_code != 200 or (isinstance(ld, dict) and ld.get('success') is False):
        raise SystemExit(f'登录失败: status={lr.status_code}, body={lr.text[:200]}')

    return s, 'password'


def get_thread_session():
    if not hasattr(thread_local, 'session'):
        thread_local.session, thread_local.mode = auth_session()
    return thread_local.session, thread_local.mode


def collect_all_mailboxes(s):
    addresses = set()

    offset = 0
    while True:
        r = s.get(
            f'{api}/api/mailboxes',
            params={'limit': LIST_PAGE_SIZE, 'offset': offset},
            timeout=REQUEST_TIMEOUT
        )
        if r.status_code >= 400:
            print(f'/api/mailboxes 请求失败: status={r.status_code}, body={r.text[:200]}')
            break

        batch = parse_list_payload(parse_json(r))
        if not batch:
            break

        for b in batch:
            if isinstance(b, dict):
                addr = str(b.get('address') or '').strip().lower()
                if addr:
                    addresses.add(addr)

        if len(batch) < LIST_PAGE_SIZE:
            break
        offset += LIST_PAGE_SIZE

    return sorted(addresses)


def delete_one(addr):
    s, mode = get_thread_session()
    try:
        dr = s.delete(
            f'{api}/api/mailboxes',
            params={'address': addr},
            timeout=REQUEST_TIMEOUT
        )
        dd = parse_json(dr)

        if dr.status_code == 200 and as_bool_deleted(dd):
            return {'address': addr, 'ok': True}
        return {
            'address': addr,
            'ok': False,
            'status': dr.status_code,
            'body': (dr.text or '')[:200]
        }
    except Exception as e:
        return {
            'address': addr,
            'ok': False,
            'status': 'EXC',
            'body': str(e)[:200]
        }


def verify_remaining(s):
    remaining = set()
    offset = 0
    while True:
        r = s.get(
            f'{api}/api/mailboxes',
            params={'limit': LIST_PAGE_SIZE, 'offset': offset},
            timeout=REQUEST_TIMEOUT
        )
        if r.status_code >= 400:
            break

        batch = parse_list_payload(parse_json(r))
        if not batch:
            break

        for b in batch:
            if isinstance(b, dict):
                addr = str(b.get('address') or '').strip().lower()
                if addr:
                    remaining.add(addr)

        if len(batch) < LIST_PAGE_SIZE:
            break
        offset += LIST_PAGE_SIZE

    return sorted(remaining)


s, auth_mode = auth_session()
print('API =', api)
print('auth_mode =', auth_mode)
print('MAX_WORKERS =', MAX_WORKERS)

addresses = collect_all_mailboxes(s)
print('mailboxes_before =', len(addresses))
if addresses:
    print('sample_before =', addresses[:10])

if not addresses:
    print('没有可删除邮箱')
    raise SystemExit(0)

ok = 0
fail = []

with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
    futures = [ex.submit(delete_one, addr) for addr in addresses]
    total = len(futures)

    for i, fut in enumerate(as_completed(futures), 1):
        result = fut.result()
        if result['ok']:
            ok += 1
        else:
            fail.append(result)
            print(f"[FAIL] {result['address']} status={result['status']} body={result.get('body','')}")

        if i % 50 == 0 or i == total:
            print(f'progress = {i}/{total}, ok={ok}, fail={len(fail)}')

print('delete_ok =', ok)
print('delete_fail =', len(fail))
if fail:
    print('delete_fail_sample =', fail[:10])

s2, _ = auth_session()
remaining = verify_remaining(s2)
print('mailboxes_after =', len(remaining))
if remaining:
    print('sample_after =', remaining[:20])