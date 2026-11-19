# [title: 携程旅行]
# [icon: https://www.ctrip.com/favicon.ico]
# [rule: ^携程(登录|签到|查询|管理|检测)$]
# [language: python]
# [disable:false]
# [public: true]
# [platform: qq,qb,wx,gw,sb,wb,tg,tb]
# [author: Echo]
# [open_source: false]
# [priority: 999999999]
# [version: 1.0.0]
# [price: 0]
# [service: ]
# [description: 携程旅行小程序签到插件。登录方式为 PC 网页版(https://passport.ctrip.com)扫码登录，用携程 APP 扫码授权。签到/任务/关注/领奖逻辑 1:1 还原自 ctrip.py。<br>指令：携程登录 / 携程签到 / 携程查询 / 携程管理 / 携程检测]

"""
【携程旅行 小程序签到 · 羽化面板插件版】

功能（1:1 还原自 qinglong-scripts/ctrip.py）：
  1. sign_in           会员签到           POST /restapi/soa2/22769/signToday
  2. get_task_list     获取任务列表       POST /restapi/soa2/22598/userTaskList
  3. todo_task         领取任务           POST /restapi/soa2/22598/todoTask
  4. handle_focus_task 完成关注任务       POST /restapi/soa2/16225/json/attention
  5. obtain_task_award 领取任务奖励       POST /restapi/soa2/22598/awardTask

登录（改为携程 PC 网页版扫码登录，与原脚本手工抓 Cookie 不同）：
  A. getQrCode     POST https://m.ctrip.com/restapi/soa2/24863/getQrCode
                   -> 返回 qrCode(凭据) 与 data(二维码内容 URL)
  B. 用 qrcode 把 data 生成二维码图片发给用户，携程 APP 扫码并确认
  C. qrCodeLogin   POST https://m.ctrip.com/restapi/soa2/22160/qrCodeLogin
                   -> 轮询：30009=等待 30010=已扫描 30011/30012=过期 0=成功
                   -> 成功后从会话中提取 Cookie 并保存到 bucket

账号数据（按当前聊天用户隔离）：
  yuhua_ctrip_user          -> 账号ID列表(JSON字符串)
  yuhua_ctrip_ck_<账号ID>   -> 登录 Cookie
"""

import json
import re
import time
import hashlib
import uuid
import io
import base64
import threading

import requests
import middleware

try:
    import qrcode
except ImportError:
    import os
    os.system("pip install qrcode[pil] -q")
    import qrcode

senderID = middleware.getSenderID()
sender = middleware.Sender(senderID)
userid = sender.getUserID()

BUCKET_USER = 'yuhua_ctrip_user'
BUCKET_CK = 'yuhua_ctrip_ck_'

M_CTRIP = 'https://m.ctrip.com'

PC_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36")
LOGIN_HEADERS = {
    'User-Agent': PC_UA,
    'Content-Type': 'application/json',
    'Origin': 'https://passport.ctrip.com',
    'Referer': 'https://passport.ctrip.com/user/login',
    'Accept': '*/*',
}

BIZ_HEADERS = {
    'User-Agent': ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36 "
                   "MicroMessenger/7.0.20.1781(0x6700143B) NetType/WIFI "
                   "MiniProgramEnv/Windows WindowsWechat/WMPF "
                   "WindowsWechat(0x63090c33)XWEB/11581"),
    'Content-Type': 'application/json',
    'cookieorigin': 'https://m.ctrip.com',
    'origin': 'https://m.ctrip.com',
    'sec-fetch-site': 'same-origin',
    'sec-fetch-mode': 'cors',
    'sec-fetch-dest': 'empty',
    'referer': ("https://m.ctrip.com/activitysetupapp/mkt/index/membersignin2021"
                "?isHideNavBar=YES&pushcode=miniprogram"
                "&mpopenid=11111111-2222-4333-8444-555555555555&ismkt=true"
                "&fromminiapp=weixin&allianceid=100000&sid=100000&sourceid=10000000"),
    'accept-language': 'zh-CN,zh;q=0.9',
}

SOA_HEAD = {
    "ctok": "", "cver": "1.0", "lang": "01", "sid": "8888",
    "syscode": "09", "auth": "", "xsid": "", "extension": []
}


def _new_session():
    s = requests.Session()
    s.headers.update(LOGIN_HEADERS)
    return s


def _qr_head(cid):
    h = dict(SOA_HEAD)
    h['cid'] = cid
    return h


def generate_qr_code_bytes(text):
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_L,
                       box_size=10, border=4)
    qr.add_data(text)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def upload_qr_to_image_host(image_bytes):
    try:
        resp = requests.post("http://yuhualhh.250666.xyz/img/api.php",
                             files={'imgfile': ('qr.png', image_bytes, 'image/png')},
                             timeout=15)
        if resp.status_code == 200:
            result = resp.json()
            if result.get('code') == 'success' and result.get('data'):
                return result['data'].get('url')
    except Exception:
        pass
    return None


def send_qr_image(text):
    try:
        img_bytes = generate_qr_code_bytes(text)
        if not img_bytes:
            return False
        url = upload_qr_to_image_host(img_bytes)
        if url:
            sender.replyImage(url)
        else:
            b64 = base64.b64encode(img_bytes).decode('utf-8')
            sender.reply(f"[CQ:image,file=base64://{b64}]")
        return True
    except Exception:
        return False


def _cookie_from_session(session):
    jar = session.cookies.get_dict()
    return "; ".join(f"{k}={v}" for k, v in jar.items() if k and v is not None)


def get_account_id(cookie):
    m = re.search(r'(?:^|;\s*)(?:uid|_userid|userid|ctripuid|userId)=([^;]+)', cookie, re.I)
    if m:
        return m.group(1).strip()
    return "id_" + hashlib.md5(cookie.encode('utf-8')).hexdigest()[:12]


def get_accounts():
    raw = middleware.bucketGet(BUCKET_USER, userid)
    if not raw:
        return []
    try:
        val = json.loads(raw)
        return val if isinstance(val, list) else []
    except Exception:
        try:
            return eval(raw)
        except Exception:
            return []


def save_accounts(accounts):
    middleware.bucketSet(BUCKET_USER, userid, json.dumps(accounts))


def mask_account(acc):
    return acc[:3] + '****' + acc[-4:] if len(acc) > 7 else acc


def scan_login(sender_obj):
    session = _new_session()
    cid = ''.join(random_digit() for _ in range(20))

    try:
        resp = session.post(
            M_CTRIP + '/restapi/soa2/24863/getQrCode',
            json={"accessCode": "PCAUTEHNTICATE", "head": _qr_head(cid)},
            headers=LOGIN_HEADERS, timeout=20)
        result = resp.json()
    except Exception as e:
        sender_obj.reply(f"❌ 获取二维码失败: {e}")
        return None

    if result.get('returnCode') != 0:
        sender_obj.reply(f"❌ 获取二维码失败: {result.get('message', result)}")
        return None

    qr_code = result.get('qrCode')
    qr_url = result.get('data')
    if not qr_code or not qr_url:
        sender_obj.reply("❌ 二维码数据异常，请重试")
        return None

    if not send_qr_image(qr_url):
        sender_obj.reply("❌ 二维码生成失败，请重试")
        return None

    sender_obj.reply(
        "=====扫码登录=====\n"
        "📱 请使用【携程 APP】扫描上方二维码\n"
        "   APP 内：我的 → 右上角设置 → 扫一扫\n"
        "------------------\n"
        "⚠️ 扫码后请在 APP 上点击“确认登录”\n"
        "⏰ 有效期约 2 分钟，回复 q 取消"
    )

    user_quit = {'v': False}

    def listen_quit():
        while not user_quit['v']:
            try:
                val = sender_obj.input(1000, 0, False)
                if val and val.strip().lower() == 'q':
                    user_quit['v'] = True
            except Exception:
                pass

    t = threading.Thread(target=listen_quit, daemon=True)
    t.start()

    cookie = None
    deadline = time.time() + 120
    while time.time() < deadline and not user_quit['v']:
        try:
            r = session.post(
                M_CTRIP + '/restapi/soa2/22160/qrCodeLogin',
                json={"accessCode": "PCAUTEHNTICATE", "qrCode": qr_code,
                      "head": _qr_head(cid)},
                headers=LOGIN_HEADERS, timeout=20)
            data = r.json()
        except Exception:
            time.sleep(2)
            continue

        rc = data.get('returnCode')
        if rc == 0:
            cookie = _cookie_from_session(session)
            break
        elif rc in (30011, 30012):
            sender_obj.reply("⏰ 二维码已过期，请重新发送 携程登录")
            user_quit['v'] = True
            break
        elif rc == 30010:
            pass
        time.sleep(2)

    user_quit['v'] = True
    if not cookie:
        return None
    return cookie


def random_digit():
    import random
    return str(random.randint(0, 9))


def login():
    guide = """
=====携程登录=====
[1] 携程 APP 扫码登录（PC 网页版，推荐）
[2] 手动输入 Cookie（小程序抓包）
------------------
回复数字选择，回复 q 退出"""
    sender.reply(guide)
    choice = sender.input(60000, 0, False)
    if not choice:
        sender.reply("❌ 输入超时")
        return
    if choice.strip().lower() == 'q':
        sender.reply("✅ 已退出操作")
        return

    cookie = None
    if choice.strip() == '1':
        sender.reply("⌛ 正在获取登录二维码...")
        cookie = scan_login(sender)
    elif choice.strip() == '2':
        sender.reply("=====Cookie 登录=====\n请输入完整 Cookie:\n------------------\n回复 q 退出")
        ck = sender.input(60000, 1, False)
        if not ck or ck.strip().lower() == 'q':
            sender.reply("✅ 已退出操作")
            return
        cookie = ck.strip()
    else:
        sender.reply("❌ 无效选择")
        return

    if not cookie:
        sender.reply("❌ 登录未完成或已取消")
        return

    sender.reply("🔍 正在验证 Cookie 并获取账号信息...")
    account_id, nick = resolve_identity(cookie)
    if not account_id:
        sender.reply("❌ Cookie 无效或已失效，请重新登录")
        return

    accounts = get_accounts()
    is_new = account_id not in accounts
    if is_new:
        accounts.append(account_id)
        save_accounts(accounts)

    middleware.bucketSet(BUCKET_CK + account_id, cookie)
    if nick:
        middleware.bucketSet(BUCKET_CK + account_id + '_nick', nick)

    sender.reply(
        "=====登录成功=====\n"
        f"🤪 账号: {mask_account(account_id)}\n"
        + (f"📛 昵称: {nick}\n" if nick else "")
        + f"✅ 状态: {'添加成功' if is_new else '更新成功'}\n"
        "------------------\n"
        "发送「携程签到」执行签到\n"
        "发送「携程查询」查看账号"
    )


def resolve_identity(cookie):
    headers = dict(BIZ_HEADERS)
    headers['Cookie'] = cookie
    try:
        r = requests.post(
            M_CTRIP + '/restapi/soa2/22598/userTaskList',
            json={
                "channelCode": "2H3294O46M",
                "extMap": {"mktTaskSort": ""},
                "oAuthHead": {},
                "version": "4", "osType": "android", "appVersion": "",
                "subOsType": "", "_locale": "zh-CN", "head": dict(SOA_HEAD)
            },
            headers=headers, timeout=25, verify=False)
        data = r.json()
    except Exception:
        return get_account_id(cookie), ''
    if isinstance(data, dict) and ('todoTaskList' in data or data.get('returnCode') in (0, None)):
        nick = ''
        for key in ('nickName', 'nickname', 'userName', 'memberName'):
            if data.get(key):
                nick = str(data.get(key))
                break
        return get_account_id(cookie), nick
    return None, ''


def _biz_client(cookie):
    s = requests.Session()
    s.headers.update(BIZ_HEADERS)
    s.headers['Cookie'] = cookie
    return s


def sign_in(client):
    try:
        response = client.post(M_CTRIP + '/restapi/soa2/22769/signToday', json={})
        response.raise_for_status()
        st = response.json()
        if st.get("code") == 0 and st.get("message") == "SUCCESS":
            sender.reply(f"**用户: 1**, 签到成功！✅获取{st.get('baseIntegratedPoint')}积分！")
        else:
            sender.reply(str(st.get("message")))
    except Exception as e:
        sender.reply(f"签到异常❌\n{e}")


def todo_task(client, task_id):
    try:
        response = client.post(
            M_CTRIP + '/restapi/soa2/22598/todoTask',
            json={
                "allianceid": "4897", "sid": "130026", "ouid": "", "sourceid": "",
                "pushcode": "h5main", "innersid": "", "innerouid": "",
                "channelCode": "2H3294O46M", "taskId": task_id,
                "status": 0, "done": 0, "oAuthHead": {}, "platform": "H5",
                "version": "4", "osType": "android", "appVersion": "",
                "subOsType": "", "_locale": "zh-CN", "head": dict(SOA_HEAD)
            })
        response.raise_for_status()
        response.json()
    except Exception as e:
        sender.reply(f"获取任务列表异常❌\n{e}")


def handle_focus_task(client, focus_id):
    try:
        response = client.post(
            M_CTRIP + '/restapi/soa2/16225/json/attention',
            json={"starCtripUid": focus_id, "head": dict(SOA_HEAD)})
        response.raise_for_status()
        data = response.json()
        if data.get('ResponseStatus', {}).get('Ack') == 'Success':
            sender.reply("任务完成！")
            return True
        sender.reply("任务失败！")
        return False
    except Exception as e:
        sender.reply(f"处理关注任务异常❌\n{e}")
        return False


def obtain_task_award(client, task_id):
    try:
        response = client.post(
            M_CTRIP + '/restapi/soa2/22598/awardTask',
            json={
                "channelCode": "2H3294O46M", "taskId": task_id,
                "platform": "H5", "oAuthHead": {},
                "version": "4", "osType": "android", "appVersion": "",
                "subOsType": "", "ouid": "", "sourceid": "",
                "pushcode": "h5main", "innersid": "", "innerouid": "",
                "head": dict(SOA_HEAD)
            })
        response.raise_for_status()
        data = response.json()
        if data.get('ResponseStatus', {}).get('Ack') == 'Success':
            if data.get('code') == 200:
                sender.reply("奖励领取成功！")
            elif data.get('code') == 40015:
                sender.reply(f"{data.get('message')}")
            else:
                sender.reply(f"奖励领取失败！{data}")
        else:
            sender.reply("奖励领取失败！")
    except Exception as e:
        sender.reply(f"领取奖励异常❌\n{e}")


def get_task_list(client):
    try:
        response = client.post(
            M_CTRIP + '/restapi/soa2/22598/userTaskList',
            json={
                "channelCode": "2H3294O46M",
                "extMap": {"mktTaskSort": ""},
                "oAuthHead": {},
                "version": "4", "osType": "android", "appVersion": "",
                "subOsType": "", "_locale": "zh-CN", "head": dict(SOA_HEAD)
            })
        response.raise_for_status()
        task_list = response.json()
        todo_task_list = task_list.get('todoTaskList')
        for task in todo_task_list:
            task_id = task.get('id')
            todo_task(client, task_id)
            if "关注" in task.get('eventName', ''):
                task_name = task.get('displayName')
                sender.reply(f"***开始处理 {task_name} 任务...***")
                try:
                    focus_id = task.get('h5Url').split('clientAuth=')[1].split('&')[0]
                    if handle_focus_task(client, focus_id):
                        obtain_task_award(client, task_id)
                except Exception:
                    sender.reply("⚠️ 关注任务解析失败，跳过")
    except Exception as e:
        sender.reply(f"获取任务列表异常❌\n{e}")


def run_checkin_for_account(account_id):
    cookie = middleware.bucketGet(BUCKET_CK + account_id)
    if not cookie:
        sender.reply(f"[{mask_account(account_id)}] ❌ 未找到 Cookie")
        return
    client = _biz_client(cookie)
    sender.reply(f"=====携程签到 [{mask_account(account_id)}]=====")
    sign_in(client)
    get_task_list(client)


def checkin():
    accounts = get_accounts()
    if not accounts:
        sender.reply("=====未绑定账号=====\n❌ 未找到账号\n💡 发送「携程登录」绑定\n=================")
        return
    for acc in accounts:
        run_checkin_for_account(acc)


def query_accounts():
    accounts = get_accounts()
    if not accounts:
        sender.reply("=====账号查询=====\n❌ 暂无账号\n💡 发送「携程登录」绑定")
        return
    lines = ["=====账号查询=====", f"共 {len(accounts)} 个账号：", "------------------"]
    for i, acc in enumerate(accounts, 1):
        nick = middleware.bucketGet(BUCKET_CK + acc + '_nick') or ''
        ck = middleware.bucketGet(BUCKET_CK + acc) or ''
        status = "✅有Cookie" if ck else "❌无Cookie"
        lines.append(f"{i}. {mask_account(acc)} {('(' + nick + ')') if nick else ''} {status}")
    lines.append("==================")
    sender.reply("\n".join(lines))


def manage_account():
    accounts = get_accounts()
    if not accounts:
        sender.reply("❌ 暂无账号可管理")
        return
    lines = ["=====账号管理=====", "回复序号删除对应账号，回复 q 退出", "------------------"]
    for i, acc in enumerate(accounts, 1):
        lines.append(f"[{i}] {mask_account(acc)}")
    sender.reply("\n".join(lines))
    choice = sender.input(60000, 0, False)
    if not choice or choice.strip().lower() == 'q':
        sender.reply("✅ 已退出操作")
        return
    try:
        idx = int(choice.strip()) - 1
        if 0 <= idx < len(accounts):
            removed = accounts.pop(idx)
            save_accounts(accounts)
            try:
                middleware.bucketDelete(BUCKET_CK + removed)
            except Exception:
                middleware.bucketSet(BUCKET_CK + removed, '')
            sender.reply(f"✅ 已删除账号 {mask_account(removed)}")
        else:
            sender.reply("❌ 序号无效")
    except ValueError:
        sender.reply("❌ 请输入数字")


def check_all():
    if not sender.isAdmin():
        sender.reply("❌ 需要管理员权限")
        return
    sender.reply("🔄 开始执行全账号签到检测...")
    checkin()
    sender.reply("✅ 全账号检测完成")


def main():
    try:
        msg = sender.getMessage().strip()
    except Exception:
        sender.setContinue()
        return

    if '登录' in msg:
        login()
    elif '签到' in msg:
        checkin()
    elif '查询' in msg:
        query_accounts()
    elif '管理' in msg:
        manage_account()
    elif '检测' in msg:
        check_all()
    else:
        sender.setContinue()


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        try:
            sender.reply(f"❌ 运行出错: {e}")
        except Exception:
            pass
