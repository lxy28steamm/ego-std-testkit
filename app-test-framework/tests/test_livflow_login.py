import os

import pytest

from test_livflow_guest import guest_flow


@pytest.mark.device
@pytest.mark.flow_login
def test_selected_account_login(guest_flow, request):
    flow = guest_flow
    flow.item = request.node
    assert os.environ.get('LIVFLOW_TEST_PHONE') and os.environ.get('LIVFLOW_TEST_PASSWORD'), '请先选择本机测试账号'
    flow.prepare_login_form()
    flow.sign_in(require_automatic=True)
    flow.ui.tap('任务')
    flow.ui.wait(
        lambda text: '任务中心' in text and not any(
            label in text for label in ('请先登录', '登录已失效', '登录已过期')),
        message='等待登录后的任务中心',
    )
    flow.note('已用所选测试账号登录，并回读任务页')
