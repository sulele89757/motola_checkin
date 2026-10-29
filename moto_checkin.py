"""
摩托啦签到

cron: 0 8 * * *
"""

import requests
import json
import os
import sys
import re
import base64
from html.parser import HTMLParser

class BalanceParser(HTMLParser):
    """用标准库解析 <div class="balance-info"> 内的 <div class="fs-2"> 文本，避免依赖 bs4"""
    def __init__(self):
        super().__init__()
        self._stack = []          # div class 列表栈，用于正确处理嵌套
        self._in_balance = False
        self._in_fs2 = False
        self.balance_text = None

    def handle_starttag(self, tag, attrs):
        if tag != 'div':
            return
        classes = dict(attrs).get('class', '').split()
        self._stack.append(classes)
        if 'balance-info' in classes:
            self._in_balance = True
        if self._in_balance and 'fs-2' in classes:
            self._in_fs2 = True

    def handle_data(self, data):
        if self._in_fs2 and self.balance_text is None:
            self.balance_text = data.strip()

    def handle_endtag(self, tag):
        if tag == 'div' and self._stack:
            classes = self._stack.pop()
            if 'fs-2' in classes:
                self._in_fs2 = False
            if 'balance-info' in classes:
                self._in_balance = False


# 站点配置
SITES = {
    "motola": {
        "name": "Motola",
        "domain": "www.motola.cn",
        "cookie_env": "MOTOLA_COOKIE",
        "user_env": "MOTOLA_USER",
        "pass_env": "MOTOLA_PASS",
        "ddddocr_url": "http://ddddocr-fastapi:8000/ocr"
    },
    "wrqt": {
        "name": "误入骑途",
        "domain": "www.wrqt.cn",
        "cookie_env": "WRQT_COOKIE",
        "user_env": "WRQT_USER",
        "pass_env": "WRQT_PASS",
        "ddddocr_url": "http://ddddocr-fastapi:8000/ocr"
    }
}

# 当前选择的站点
current_site = None

def get_site_config(site_key):
    """获取站点配置"""
    global current_site
    if site_key not in SITES:
        raise ValueError(f"未知的站点: {site_key}, 可用站点: {list(SITES.keys())}")
    current_site = SITES[site_key]
    return current_site

def get_env(env_name):
    """获取环境变量"""
    value = os.getenv(env_name)
    if not value:
        raise ValueError(f"未设置环境变量 {env_name}")
    return value

# 青龙通知模块
def ql_send(title, message):
    """
    青龙面板通知函数
    """
    try:
        QLAPI.notify(title, message)
    except Exception as e:
        print(f"通知发送失败: {e}")

def get_nonce(cookie):
    """
    从网站首页提取 nonce 值
    """
    if not current_site:
        raise Exception("未设置站点配置")
    
    url = f"https://{current_site['domain']}/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "keep-alive"
    }
    print(f"[Nonce] 请求首页: {url}")
    print(f"[Nonce] Cookie: {cookie[:50]}...")
    try:
        resp = requests.get(url, headers=headers, timeout=30)
        print(f"[Nonce] 响应状态码: {resp.status_code}")
        resp.raise_for_status()  # 检查HTTP错误
        resp.encoding = 'utf-8'
        html = resp.text
        print(f"[Nonce] 页面长度: {len(html)}")

        # 模式1: 直接匹配 "ajax_nonce":"xxxxx"
        match = re.search(r'["\']ajax_nonce["\']\s*:\s*["\']([^"\']+)["\']', html)
        if match:
            nonce = match.group(1)
            print(f"✅ 从 {url} 获取到 nonce: {nonce}")
            return nonce

        # 模式2: 提取 var zb = {...} 并解析
        match = re.search(r'var\s+zb\s*=\s*({[^}]*});', html, re.DOTALL)
        if match:
            zb_json_str = match.group(1)
            # 清理 JSON 字符串
            cleaned_json = re.sub(r'//.*$', '', zb_json_str)
            cleaned_json = re.sub(r'/\*.*?\*/', '', cleaned_json)
            cleaned_json = re.sub(r'\s+', ' ', cleaned_json)
            try:
                zb_obj = json.loads(cleaned_json)
                if 'ajax_nonce' in zb_obj:
                    nonce = zb_obj['ajax_nonce']
                    print(f"✅ 从 zb 对象 JSON 解析到 nonce: {nonce}")
                    return nonce
            except Exception as e:
                print(f"❌ JSON 解析失败 (zb): {e}")

        # 其他备用模式...
        patterns = [
            r"var\s+zb_user_qiandao_nonce\s*=\s*['\"]([^'\"]+)['\"]",
            r"data-nonce=['\"]([^'\"]+)['\"]",
            r'<input[^>]*name=[\'"]_wpnonce[\'"][^>]*value=[\'"]([^\'"]+)[\'"]',
            r'<input[^>]*name=[\'"]zb_user_qiandao_nonce[\'"][^>]*value=[\'"]([^\'"]+)[\'"]',
        ]

        for pattern in patterns:
            match = re.search(pattern, html, re.IGNORECASE)
            if match:
                nonce = match.group(1)
                print(f"自动获取到 nonce: {nonce}")
                return nonce

        print("未能在首页找到 nonce")
        return None
    except Exception as e:
        print(f"获取 nonce 时出错: {e}")
        return None

def get_captcha_img(cookie, nonce):
    """
    获取验证码图片，返回(图片base64, 新增的cookie)
    """
    if not current_site:
        raise Exception("未设置站点配置")
    
    url = f"https://{current_site['domain']}/wp-admin/admin-ajax.php"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Accept": "*/*",
        "Host": current_site['domain'],
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest"
    }
    data = {
        "action": "zb_get_captcha_img",
        "nonce": nonce
    }
    print(f"[验证码] 请求URL: {url}")
    print(f"[验证码] 请求Data: {data}")
    print(f"[验证码] 请求Headers: {headers}")
    try:
        response = requests.post(url, headers=headers, data=data, timeout=30)
        print(f"[验证码] 响应状态码: {response.status_code}")
        print(f"[验证码] 响应内容: {response.text[:200]}")
        
        # 从响应头中获取新设置的cookie
        set_cookie = response.headers.get('Set-Cookie', '')
        new_cookie = ''
        if set_cookie:
            # 提取 _zb_img_captcha_code
            for item in set_cookie.split(','):
                if '_zb_img_captcha_code' in item:
                    new_cookie = item.split(';')[0].strip()
                    print(f"[验证码] 新增Cookie: {new_cookie}")
                    break
        
        resp_json = response.json()
        if resp_json.get('status') == 1:
            img_data = resp_json.get('msg', '')
            # 清理 "data:image/jpeg;base64," 前缀
            if 'base64,' in img_data:
                img_data = img_data.split('base64,')[1]
            print(f"✅ 获取验证码成功")
            return img_data, new_cookie
        else:
            print(f"❌ 获取验证码失败: {resp_json.get('msg')}")
            return None, None
    except Exception as e:
        print(f"获取验证码出错: {e}")
        return None, None

def recognize_captcha(base64_img):
    """
    使用ddddocr识别验证码
    """
    if not current_site:
        raise Exception("未设置站点配置")
    
    url = current_site.get('ddddocr_url', 'http://localhost:8181/ocr')
    data = {
        "file": "",
        "image": base64_img,
        "probability": "false",
        "charsets": "",
        "png_fix": "false"
    }
    print(f"[识别] ddddocr API: {url}")
    print(f"[识别] 验证码Base64长度: {len(base64_img)}")
    try:
        response = requests.post(url, data=data, timeout=30)
        print(f"[识别] 响应状态码: {response.status_code}")
        print(f"[识别] 响应内容: {response.text[:200]}")
        resp_json = response.json()
        if resp_json.get('code') == 200:
            captcha_code = resp_json.get('data', '')
            print(f"✅ 识别验证码成功: {captcha_code}")
            return captcha_code
        else:
            print(f"❌ 识别验证码失败: {resp_json}")
            return None
    except Exception as e:
        print(f"识别验证码出错: {e}")
        return None

def do_login(cookie, nonce, captcha_code):
    """
    执行登录操作
    """
    if not current_site:
        raise Exception("未设置站点配置")
    
    url = f"https://{current_site['domain']}/wp-admin/admin-ajax.php"
    
    # 从当前站点配置获取用户名和密码
    user_name = get_env(current_site['user_env'])
    user_password = get_env(current_site['pass_env'])
    
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Accept": "*/*",
        "Host": current_site['domain'],
        "Connection": "keep-alive",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://{current_site['domain']}/"
    }
    
    data = {
        "nonce": nonce,
        "user_name": user_name,
        "user_password": user_password,
        "captcha_code": captcha_code,
        "remember": "on",
        "action": "zb_user_login"
    }
    
    print(f"[登录] 请求URL: {url}")
    print(f"[登录] nonce: {nonce}")
    print(f"[登录] user_name: {user_name}")
    print(f"[登录] captcha_code: {captcha_code}")
    print(f"[登录] 请求Headers: {headers}")
    
    try:
        response = requests.post(url, headers=headers, data=data, timeout=30)
        print(f"[登录] 响应状态码: {response.status_code}")
        print(f"[登录] 响应Headers: {response.headers}")
        print(f"[登录] 响应内容: {response.text[:300]}")
        resp_json = response.json()
        print(f"登录响应: {resp_json}")
        
        # 获取登录后的Cookie
        cookie_jar = response.cookies
        login_cookie = "; ".join([f"{c.name}={c.value}" for c in cookie_jar])
        
        # 从响应头中获取Set-Cookie
        set_cookie = response.headers.get('Set-Cookie', '')
        if set_cookie:
            # 解析Set-Cookie
            cookies = []
            for item in set_cookie.split(','):
                if '=' in item:
                    parts = item.split(';')
                    cookie_part = parts[0].strip()
                    cookies.append(cookie_part)
            login_cookie = "; ".join(cookies)
        
        if resp_json.get('status') == 1:
            print(f"✅ 登录成功")
            return login_cookie, "登录成功"
        else:
            msg = resp_json.get('msg', '未知错误')
            print(f"❌ 登录失败: {msg}")
            return None, msg
    except Exception as e:
        print(f"登录出错: {e}")
        return None, str(e)

# def do_login_with_cookie(cookie, max_captcha_retries=10):
    """
    尝试登录并返回新的cookie（包含从首页获取的nonce）
    当验证码识别错误时，自动重试整个流程
    """
    print("=" * 50)
    print("开始重新登录流程...")
    print("=" * 50)
    
    for captcha_retry in range(max_captcha_retries):
        # 先获取nonce
        print("[登录流程] Step 1: 获取Nonce")
        nonce = get_nonce(cookie)
        if not nonce:
            print("[登录流程] ❌ 获取Nonce失败")
            return None
        print(f"[登录流程] ✅ 获取Nonce成功: {nonce[:20]}...")
        
        # 获取验证码（同时获取新增的cookie）
        print("[登录流程] Step 2: 获取验证码图片")
        captcha_base64, captcha_cookie = get_captcha_img(cookie, nonce)
        if not captcha_base64:
            print("[登录流程] ❌ 获取验证码失败")
            return None
        print(f"[登录流程] ✅ 获取验证码成功, Base64长度: {len(captcha_base64)}")
        
        # 合并cookie，添加 _zb_img_captcha_code
        if captcha_cookie:
            full_cookie = f"{cookie}; {captcha_cookie}"
            print(f"[登录流程] 合并后的Cookie: {full_cookie[:80]}...")
        else:
            full_cookie = cookie
            print(f"[登录流程] 未获取到新增Cookie，使用原Cookie")
        
        # 识别验证码
        print("[登录流程] Step 3: 识别验证码")
        captcha_code = recognize_captcha(captcha_base64)
        if not captcha_code:
            print("[登录流程] ❌ 识别验证码失败")
            return None
        print(f"[登录流程] ✅ 识别成功, 验证码: {captcha_code}")
        
        # 执行登录（使用合并后的cookie）
        print("[登录流程] Step 4: 执行登录")
        new_cookie, login_msg = do_login(full_cookie, nonce, captcha_code)
        
        if new_cookie:
            print(f"[登录流程] ✅ 登录成功, 新Cookie: {new_cookie[:50]}...")
            return new_cookie
        else:
            print(f"[登录流程] ❌ 登录失败: {login_msg}")
            
            # 检查是否是验证码错误，如果是则重试
            if '验证码错误' in str(login_msg):
                print(f"[登录流程] ⚠️ 验证码识别错误，第 {captcha_retry + 1}/{max_captcha_retries} 次重试...")
                continue
            else:
                # 其他错误，直接返回
                return None
    
    print("[登录流程] ❌ 验证码识别重试次数耗尽")
    return None

def do_login_with_cookie(cookie, max_captcha_retries=10):
    """
    尝试登录并返回新的cookie（包含从首页获取的nonce）
    当验证码识别错误时，自动重试整个流程
    """
    print("=" * 50)
    print("开始重新登录流程...")
    print("=" * 50)
    
    for captcha_retry in range(max_captcha_retries):
        # 先获取nonce
        print("[登录流程] Step 1: 获取Nonce")
        nonce = get_nonce(cookie)
        if not nonce:
            print("[登录流程] ❌ 获取Nonce失败")
            return None
        print(f"[登录流程] ✅ 获取Nonce成功: {nonce[:20]}...")
        
        # 获取验证码（同时获取新增的cookie）
        print("[登录流程] Step 2: 获取验证码图片")
        captcha_base64, captcha_cookie = get_captcha_img(cookie, nonce)
        if not captcha_base64:
            print("[登录流程] ❌ 获取验证码失败")
            return None
        print(f"[登录流程] ✅ 获取验证码成功, Base64长度: {len(captcha_base64)}")
        
        # 合并cookie，添加 _zb_img_captcha_code
        if captcha_cookie:
            full_cookie = f"{cookie}; {captcha_cookie}"
            print(f"[登录流程] 合并后的Cookie: {full_cookie[:80]}...")
        else:
            full_cookie = cookie
            print(f"[登录流程] 未获取到新增Cookie，使用原Cookie")
        
        # 识别验证码
        print("[登录流程] Step 3: 识别验证码")
        captcha_code = recognize_captcha(captcha_base64)
        if not captcha_code:
            print("[登录流程] ❌ 识别验证码失败")
            return None
        print(f"[登录流程] ✅ 识别成功, 验证码: {captcha_code}")
        
        # 执行登录（使用合并后的cookie）
        print("[登录流程] Step 4: 执行登录")
        new_cookie, login_msg = do_login(full_cookie, nonce, captcha_code)
        
        if new_cookie:
            print(f"[登录流程] ✅ 登录成功, 新Cookie: {new_cookie[:50]}...")
            
            # 清理cookie中的无效字段
            cleaned_cookie = clean_invalid_cookies(new_cookie)
            print(f"[登录流程] 清理后的Cookie: {cleaned_cookie[:50]}...")
            
            return cleaned_cookie
        else:
            print(f"[登录流程] ❌ 登录失败: {login_msg}")
            
            # 检查是否是验证码错误，如果是则重试
            if '验证码错误' in str(login_msg):
                print(f"[登录流程] ⚠️ 验证码识别错误，第 {captcha_retry + 1}/{max_captcha_retries} 次重试...")
                continue
            else:
                # 其他错误，直接返回
                return None
    
    print("[登录流程] ❌ 验证码识别重试次数耗尽")
    return None

def clean_invalid_cookies(cookie_str):
    """
    清理cookie中的无效字段
    删除 _zb_img_captcha_code=deleted 和时间戳字段
    """
    if not cookie_str:
        return cookie_str
    
    # 将cookie按分号分割
    cookies = [c.strip() for c in cookie_str.split(';') if c.strip()]
    
    # 过滤掉无效的cookie项
    cleaned_cookies = []
    for cookie_item in cookies:
        # 跳过 _zb_img_captcha_code=deleted
        if '_zb_img_captcha_code=deleted' in cookie_item:
            print(f"[清理Cookie] 移除无效项: {cookie_item}")
            continue
        
        # 跳过日期格式的时间戳（如: 01-Jan-1970 00:00:01 GMT）
        if re.match(r'\d{2}-[A-Z][a-z]{2}-\d{4}\s+\d{2}:\d{2}:\d{2}\s+GMT', cookie_item):
            print(f"[清理Cookie] 移除时间戳: {cookie_item}")
            continue
        
        cleaned_cookies.append(cookie_item)
    
    # 重新组合cookie字符串
    cleaned_cookie = '; '.join(cleaned_cookies)
    print(f"[清理Cookie] 原始长度: {len(cookie_str)}, 清理后长度: {len(cleaned_cookie)}")
    
    return cleaned_cookie

def set_env_variable(name, value):
    """
    设置青龙面板环境变量
    """
    try:
        # 先获取现有的环境变量信息
        env_list = QLAPI.getEnvs({"searchValue": name})
        target_env = None
        
        if env_list and env_list.get('data'):
            for env in env_list['data']:
                if env.get('name') == name:
                    target_env = env
                    break
        
        if target_env:
            # 补全所有字段后更新
            result = QLAPI.updateEnv({
                "env": {
                    "id": target_env["id"],
                    "name": target_env["name"],
                    "value": value,
                    "remarks": target_env.get("remarks", ""),
                    "status": target_env.get("status", 1)
                }
            })
            print(f"✅ 已更新环境变量 {name}, result: {result}")
        else:
            print(f"⚠️ 未找到环境变量 {name}，尝试创建...")
            # 如果不存在则创建
            result = QLAPI.setEnv({
                "name": name,
                "value": value
            })
            print(f"✅ 已创建环境变量 {name}, result: {result}")
    except Exception as e:
        print(f"⚠️ 无法自动更新环境变量 {name}: {e}")
        print(f"请手动更新环境变量，新Cookie: {value[:50]}...")

def get_user_balance(cookie):
    """
    访问用户积分页面，解析并返回当前积分余额
    """
    if not current_site:
        raise Exception("未设置站点配置")
    
    url = f"https://{current_site['domain']}/user/coin/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "Cookie": cookie,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Connection": "keep-alive"
    }
    
    print(f"[积分查询] 请求URL: {url}")
    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        response.encoding = 'utf-8'
        html = response.text
        
        # 使用标准库 html.parser 解析 HTML（无需第三方依赖，cron/手动环境一致）
        parser = BalanceParser()
        parser.feed(html)

        text = parser.balance_text
        if not text:
            print("❌ 未找到 balance-info > fs-2 元素")
            return None

        # 从文本中提取数字部分（如 "7.5积分" -> "7.5"）
        match = re.search(r'([\d.]+)', text)
        if match:
            balance = match.group(1)
            print(f"✅ 获取积分余额成功: {balance}积分")
            return balance
        else:
            print(f"❌ 无法从文本中提取积分数值: {text}")
            return None
            
    except Exception as e:
        print(f"获取积分余额出错: {e}")
        return None

def do_checkin_with_retry(cookie, retry_count=2):
    """
    执行签到，带重试逻辑
    返回: (是否成功, 消息, 新cookie, 积分余额)
    """
    if not current_site:
        raise Exception("未设置站点配置")
    
    url = f"https://{current_site['domain']}/wp-admin/admin-ajax.php"
    current_site_name = current_site['name']

    for attempt in range(retry_count + 1):
        # 获取nonce
        nonce = get_nonce(cookie)
        if not nonce:
            return False, "❌ 无法获取 nonce，请检查 Cookie 是否有效", None, None
        
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
            "Cookie": cookie,
            "Accept": "*/*",
            "Host": current_site['domain'],
            "Connection": "keep-alive",
            "Referer": f"https://{current_site['domain']}/"
        }
        
        data = {
            "action": "zb_user_qiandao",
            "nonce": nonce
        }
        
        try:
            response = requests.post(url, headers=headers, data=data, timeout=30)
            
            # 检查响应状态
            if response.status_code != 200:
                raise requests.exceptions.HTTPError(f"HTTP {response.status_code}: {response.text[:100]}")
            
            # 尝试解析JSON
            try:
                resp_json = response.json()
            except json.JSONDecodeError:
                # JSON解析失败，可能是HTML错误页面
                raise Exception(f"响应不是有效JSON: {response.text[:200]}")
            
            # 检查resp_json是否是字典
            if not isinstance(resp_json, dict):
                raise Exception(f"响应格式错误: {type(resp_json)}, 内容: {str(resp_json)[:100]}")
            
            status = resp_json.get('status')
            msg = resp_json.get('message') or resp_json.get('msg') or ''
            
            # 确保status是整数类型
            if isinstance(status, str):
                try:
                    status = int(status)
                except (ValueError, TypeError):
                    status = -1
            
            if status == 1:
                # 签到成功，获取积分余额
                balance = get_user_balance(cookie)
                if balance:
                    message = f"✅ 【{current_site_name}】签到成功！\n{msg}\n💰 当前积分余额: {balance}积分"
                else:
                    message = f"✅ 【{current_site_name}】签到成功！\n{msg}"
                return True, message, cookie, balance
            elif status == 0:
                # 今日已签到，也获取积分余额
                balance = get_user_balance(cookie)
                if balance:
                    message = f"ℹ️ 【{current_site_name}】今日已签到\n{msg}\n💰 当前积分余额: {balance}积分"
                else:
                    message = f"ℹ️ 【{current_site_name}】今日已签到\n{msg}"
                return True, message, cookie, balance
            else:
                # 签到失败，检查是否需要重新登录
                if '未登录' in str(msg) or '登录' in str(msg) or status == -1 or status == 0:
                    if attempt < retry_count:
                        print(f"⚠️ 第 {attempt + 1} 次签到失败，尝试重新登录...")
                        new_cookie = do_login_with_cookie(cookie)
                        if new_cookie:
                            print(f"✅ 重新登录成功，获得新Cookie")
                            cookie = new_cookie
                            continue  # 重试签到
                        else:
                            return False, f"❌ 签到失败，重新登录失败\n{msg}", None, None
                    else:
                        return False, f"❌ 签到重试次数耗尽\n{msg}", None, None
                else:
                    return False, f"⚠️ 【{current_site_name}】签到结果未知\n{msg}", None, None
                    
        except requests.exceptions.HTTPError as e:
            # HTTP错误，如403、401等，可能cookie失效
            if attempt < retry_count:
                print(f"⚠️ HTTP错误: {e}，尝试重新登录...")
                new_cookie = do_login_with_cookie(cookie)
                if new_cookie:
                    print(f"✅ 重新登录成功，获得新Cookie")
                    cookie = new_cookie
                    continue
                else:
                    return False, f"❌ HTTP错误，重新登录失败: {e}", None, None
            else:
                return False, f"❌ HTTP错误，重试耗尽: {e}", None, None
                
        except requests.exceptions.RequestException as e:
            # 其他网络错误
            if attempt < retry_count:
                print(f"⚠️ 网络请求错误: {e}，尝试重新登录...")
                new_cookie = do_login_with_cookie(cookie)
                if new_cookie:
                    print(f"✅ 重新登录成功，获得新Cookie")
                    cookie = new_cookie
                    continue
                else:
                    return False, f"❌ 网络请求错误，重新登录失败: {e}", None, None
            else:
                return False, f"❌ 网络请求错误，重试耗尽: {e}", None, None
                
        except Exception as e:
            return False, f"❌ 签到过程出错: {e}", None, None
    
    return False, "❌ 签到重试次数耗尽", None, None

def site_checkin(site_key):
    """
    通用站点签到函数
    """
    # 初始化站点配置
    site_config = get_site_config(site_key)
    site_name = site_config['name']
    cookie_env = site_config['cookie_env']
    
    print(f"开始执行 {site_name} 签到任务...")
    
    # 从环境变量读取 Cookie
    cookie = os.getenv(cookie_env)
    if not cookie:
        error_msg = f"❌ 未设置环境变量 {cookie_env}，请检查青龙配置"
        print(error_msg)
        ql_send(f"{site_name}签到错误", error_msg)
        return
    
    print("开始执行签到...")
    success, message, new_cookie, balance = do_checkin_with_retry(cookie, retry_count=2)
    
    # 如果获得新cookie，更新环境变量
    if new_cookie and new_cookie != cookie:
        print(f"✅ 获得新Cookie，更新环境变量...")
        set_env_variable(cookie_env, new_cookie)
    
    ql_send(f"{site_name}签到通知" if success else f"{site_name}签到错误", message)

if __name__ == "__main__":
    # 从命令行参数或环境变量获取站点名称
    # 用法: python script.py motola 或 python script.py wrqt
    site_key = sys.argv[1] if len(sys.argv) > 1 else os.getenv("SITE_KEY", "motola")
    
    print(f"选择的站点: {site_key}")
    site_checkin(site_key)
    print("签到任务执行完成")
