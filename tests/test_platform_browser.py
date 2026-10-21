"""Opt-in browser acceptance against real isolated API/Worker/DB/Redis processes."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

from tests.test_platform_http import platform, port, wait_until, ROOT

pytestmark = pytest.mark.skipif(os.getenv('ECOM_INTEGRATION') != '1', reason='isolated browser acceptance only')


def test_customer_staff_questions_pagination_and_live_updates(platform):
    from playwright.sync_api import sync_playwright, expect
    from app.api.auth import hash_password
    import httpx

    ui_port = port()
    ui_url = f'http://127.0.0.1:{ui_port}'
    users = ','.join(f'{user}:{hash_password("fixture-pass")}' for user in ('customer', 'staff'))
    _, api_url = platform['start']('api', CORS_ORIGINS=ui_url, API_AUTH_USERS=users, AUTH_COOKIE_SECURE='false')
    for role in ('dispatcher', 'worker', 'worker'):
        platform['start'](role)
    log = (platform['root'] / 'frontend.log').open('w', encoding='utf-8')
    frontend = subprocess.Popen([shutil.which('node'), str(ROOT / 'frontend/node_modules/vite/bin/vite.js'),
                                 '--host', '127.0.0.1', '--port', str(ui_port), '--strictPort'],
        cwd=ROOT / 'frontend', env={**os.environ, 'VITE_API_BASE_URL': api_url},
        stdout=log, stderr=subprocess.STDOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        def ready():
            if frontend.poll() is not None:
                raise AssertionError('frontend exited; see frontend.log')
            try: return platform['client'].get(ui_url).status_code == 200
            except httpx.TransportError: return False
        wait_until(ready)
        with sync_playwright() as playwright:
            executable = os.getenv('ECOM_BROWSER_EXECUTABLE')
            if not executable and os.name == 'nt':
                executable = r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe'
            browser = playwright.chromium.launch(headless=True, executable_path=executable,
                                                  args=['--no-proxy-server'])
            errors = []
            def login(user):
                context = browser.new_context(viewport={'width': 1280, 'height': 1000})
                page = context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                page.goto(ui_url)
                page.get_by_label('用户名', exact=True).fill(user)
                page.get_by_label('密码', exact=True).fill('fixture-pass')
                page.get_by_role('button', name='进入工作台').click()
                expect(page.get_by_role('button', name='转人工客服')).to_be_visible()
                return page
            customer = login('customer')
            customer.get_by_label('咨询内容').fill('查询订单')
            customer.get_by_role('button', name='发送消息', exact=True).click()
            expect(customer.locator('.support-transcript')).to_contain_text('请补充订单号', timeout=20000)
            customer.reload()
            expect(customer.locator('.support-transcript')).to_contain_text('请补充订单号', timeout=10000)
            customer.get_by_label('咨询内容').fill('ORD-A')
            customer.get_by_role('button', name='发送消息', exact=True).click()
            expect(customer.locator('.support-transcript')).to_contain_text('ORD-A', timeout=20000)
            expect(customer.locator('.support-transcript')).to_contain_text('当前状态：pending', timeout=20000)
            with platform['store'].transaction() as tx:
                c = tx.one("SELECT * FROM support_conversations WHERE user_id='customer'")
                for index in range(85):
                    platform['store']._event(tx, c, 'message', 'customer',
                        {'role': 'user', 'content': f'历史消息-{index}'})
            customer.reload()
            expect(customer.locator('.support-message')).to_have_count(50)
            customer.get_by_role('button', name='查看更早消息').click()
            expect(customer.locator('.support-transcript')).to_contain_text('查询订单')
            customer.get_by_role('button', name='转人工客服').click()
            expect(customer.get_by_role('heading', name='等待人工接入')).to_be_visible(timeout=10000)
            staff = login('staff')
            staff.get_by_role('button', name='客服工作台').click()
            staff.get_by_role('button', name='领取工单').click()
            expect(staff.get_by_label('客服回复')).to_be_visible()
            staff.get_by_label('客服回复').fill('人工客服已接管，可以继续补充信息。')
            staff.get_by_role('button', name='发送回复').click()
            expect(customer.locator('.support-transcript')).to_contain_text('人工客服已接管', timeout=10000)
            staff.get_by_label('内部备注（用户不可见）').check()
            staff.get_by_label('客服回复').fill('internal-fixture-only')
            staff.get_by_role('button', name='保存备注').click()
            expect(staff.locator('.support-transcript')).to_contain_text('internal-fixture-only')
            customer.reload()
            expect(customer.get_by_role('heading', name='人工客服', exact=True)).to_be_visible()
            expect(customer.locator('.support-transcript')).not_to_contain_text('internal-fixture-only')
            customer.screenshot(path=str(platform['root'] / 'customer-desktop.png'), full_page=True)
            staff.screenshot(path=str(platform['root'] / 'staff-desktop.png'), full_page=True)
            customer.set_viewport_size({'width': 390, 'height': 844})
            assert customer.evaluate('document.documentElement.scrollWidth <= window.innerWidth')
            customer.screenshot(path=str(platform['root'] / 'customer-mobile.png'), full_page=True)
            assert not errors, errors
            browser.close()
    finally:
        frontend.terminate()
        try: frontend.wait(timeout=8)
        except subprocess.TimeoutExpired:
            frontend.kill(); frontend.wait(timeout=5)
        log.close()
