"""Account selector with a separate account list for each patrol page."""
from uuid import uuid4

from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit,
    QMessageBox, QPushButton, QWidget,
)

from app_patrol_credentials import load_accounts, save_accounts


class AccountCombo(QComboBox):
    def showPopup(self):
        self.parentWidget().refresh()
        super().showPopup()


class PatrolAccountPicker(QWidget):
    def __init__(self, scope, parent=None):
        super().__init__(parent)
        self.scope = scope
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.combo = AccountCombo(self)
        layout.addWidget(self.combo, 1)
        for label, handler in (('新增', lambda: self.edit(add=True)), ('编辑', self.edit), ('删除', self.remove)):
            button = QPushButton(label, self)
            button.clicked.connect(handler)
            layout.addWidget(button)
        self.refresh(first=True)

    def refresh(self, selected=None, *, first=False):
        selected = self.combo.currentData() if selected is None else selected
        try:
            accounts = load_accounts(self.scope)
        except (OSError, ValueError):
            accounts = []
        self.combo.clear()
        self.combo.addItem('不使用本机账号', '')
        for account in accounts:
            self.combo.addItem(f"{account['name']} · {account['PHONE']}", account['id'])
        if first and len(accounts) == 1:
            selected = accounts[0]['id']
        self.combo.setCurrentIndex(max(0, self.combo.findData(selected)))

    def selected_account(self):
        selected = self.combo.currentData()
        if not selected:
            return {}
        return next((account for account in load_accounts(self.scope) if account['id'] == selected), {})

    def edit(self, *, add=False):
        try:
            account = {} if add else self.selected_account()
            if not add and not account:
                QMessageBox.information(self, '测试账号', '请先选择要编辑的账号。')
                return
            dialog = QDialog(self)
            dialog.setWindowTitle('本机测试账号')
            form = QFormLayout(dialog)
            fields = {}
            for key, label in (('name', '账号名称'), ('PHONE', '账号或手机号'),
                               ('PASSWORD', '密码'), ('ROLE', 'APP 登录身份（可选）')):
                entry = QLineEdit(account.get(key, ''), dialog)
                if key == 'PASSWORD':
                    entry.setEchoMode(QLineEdit.Password)
                fields[key] = entry
                form.addRow(label, entry)
            buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, dialog)
            form.addRow(buttons)

            def save():
                values = {key: entry.text() for key, entry in fields.items()}
                values['PHONE'] = values['PHONE'].strip()
                values['name'] = values['name'].strip() or values['PHONE']
                if not values['PHONE'] or not values['PASSWORD']:
                    QMessageBox.warning(dialog, '测试账号不完整', '请填写账号和密码。')
                    return
                values['id'] = account.get('id') or uuid4().hex
                try:
                    accounts = load_accounts(self.scope)
                    save_accounts(self.scope, [row for row in accounts if row['id'] != values['id']] + [values])
                except (OSError, ValueError):
                    QMessageBox.warning(dialog, '保存失败', '无法写入本机测试账号列表。')
                    return
                self.refresh(values['id'])
                dialog.accept()

            buttons.accepted.connect(save)
            buttons.rejected.connect(dialog.reject)
            dialog.exec()
        except (OSError, ValueError):
            QMessageBox.warning(self, '读取失败', '无法读取本机测试账号列表。')

    def remove(self):
        try:
            account = self.selected_account()
            if not account:
                return
            if QMessageBox.question(self, '删除本机账号', f"从列表删除「{account['name']}」？") != QMessageBox.Yes:
                return
            accounts = load_accounts(self.scope)
            save_accounts(self.scope, [row for row in accounts if row['id'] != account['id']])
            self.refresh('')
        except (OSError, ValueError):
            QMessageBox.warning(self, '删除失败', '无法更新本机测试账号列表。')
