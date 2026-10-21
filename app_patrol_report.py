"""Offline, self-contained human-readable patrol report. Never changes results."""
from collections import Counter
from html import escape
from pathlib import Path
from urllib.parse import quote
import re


STATUS = {'通过': 'pass', '失败': 'fail', '待确认': 'review', '未测': 'skip', '未执行': 'pending'}
CAPTIONS = {'connected_preview': '设备连接与双目预览', 'task_detail_statistics': '任务详情与统计',
            'record_started': '开始采集', 'record_return_from_background': '后台返回后仍在采集',
            'record_stopped': '采集已结束', 'upload_selected': '本轮选中的测试文件',
            'upload_complete': '上传结果', 'duplicate_upload_blocked': '重复上传已拦截',
            'failure': '失败时的页面', 'task_search': '任务搜索', 'task_search_reset': '重置搜索',
            'parameters_restored': '参数恢复结果'}


def short_detail(row):
    detail = str(row.get('detail') or '')
    errors = re.findall(r'^E\s+(?:AssertionError:\s*)?(.*)$', detail, re.M)
    if errors:
        return errors[-1][:240]
    if row.get('status') == '通过':
        return '本轮自动断言通过' if detail == '自动断言通过' else detail[:240] or '本轮已记录通过'
    return detail[:240] or {'未执行': '本项尚未执行', '未测': '本轮未完成检查',
                           '待确认': '需要人工复核', '失败': '请查看检查详情和证据'}.get(row.get('status'), '')


def render_report(session, mode_title):
    folder = Path(session['folder'])
    cases = session.get('cases', [])
    counts = Counter(x.get('status', '未执行') for x in cases)
    total = len(cases)
    passed = counts['通过']
    unresolved = counts['未测'] + counts['未执行']
    state = session.get('state', '未开始')
    active = state in ('运行中', '未开始', '等待人工操作', '人工巡检进行中')
    conclusion = ('正在检查，结果尚未定稿' if active else '发现失败项，请优先处理' if counts['失败'] else
                  '部分项目尚未完成' if unresolved else '存在待确认项，请复核' if counts['待确认'] else
                  '本轮已执行项目全部通过' if total else '本轮尚无检查结果')
    tone = 'fail' if counts['失败'] else 'review' if counts['待确认'] or unresolved or active or not total else 'pass'
    esc = lambda x: escape(str(x), quote=True)
    links = '<a href="cases.csv" download>导出清单 CSV</a><a href="session.json">原始结果</a>'
    if (folder/'report.html').is_file():
        links += '<a href="report.html">技术详细报告</a>'
    if (folder/'recording_comparison.json').is_file():
        links += '<a href="recording_comparison.json">文件前后对比</a>'
    cards = ''.join(f'<div class="metric {STATUS.get(status, "pending")}"><span>{status}</span><strong>{number}</strong></div>'
                    for status, number in [('检查项目', total), ('通过', passed), ('失败', counts['失败']),
                                           ('待确认', counts['待确认']), ('未完成', unresolved)])
    issues = [r for r in cases if r.get('status') in ('失败', '待确认')]
    issues_html = ''.join(f'<article class="issue"><span class="badge {STATUS[r["status"]]}">{r["status"]}</span>'
                          f'<a href="#case-{cases.index(r)}">{esc(r.get("title", "检查项"))}</a>'
                          f'<p>{esc(short_detail(r))}</p></article>' for r in issues)
    if not issues:
        issues_html = '<p class="muted">当前没有失败或待确认项。' + ('仍有未完成项目，请勿据此判定全部功能通过。' if unresolved or active else '各项结果见下方检查列表。') + '</p>'
    rows = []
    order = {'失败': 0, '待确认': 1, '未测': 2, '未执行': 3, '通过': 4}
    for index, row in sorted(enumerate(cases), key=lambda pair: order.get(pair[1].get('status'), 5)):
        status = row.get('status', '未执行')
        detail = str(row.get('detail') or '')
        tech = ('<details><summary>查看完整记录</summary><pre>' + esc(detail) + '</pre></details>') if detail and detail != '自动断言通过' else ''
        rows.append(f'<tr id="case-{index}" data-status="{esc(status)}"><td><span class="badge {STATUS.get(status,"pending")}">{esc(status)}</span></td>'
                    f'<td><strong>{esc(row.get("title", ""))}</strong><small>{esc(row.get("module", ""))}</small></td>'
                    f'<td>{esc(short_detail(row))}{tech}<details><summary>检查编号</summary><code>{esc(row.get("id", ""))}</code></details></td></tr>')
    evidence = []
    for p in sorted(folder.glob('*.png')):
        caption = re.sub(r'^flow_\d+_', '', p.stem)
        caption = CAPTIONS.get(caption, caption)
        uri = quote(p.name)
        xml = p.with_suffix('.xml')
        extra = f'<a href="{quote(xml.name)}">页面结构</a>' if xml.is_file() else ''
        evidence.append(f'<figure><a href="{uri}" target="_blank" rel="noopener"><img src="{uri}" loading="lazy" alt="{esc(caption)}"></a>'
                        f'<figcaption>{esc(caption)}<small>{esc(p.name)}</small>{extra}</figcaption></figure>')
    waiting = session.get('waiting', {})
    target = session.get('target', {})
    target_html = ''.join(f'<span>{label} {esc(target[key])}</span>' for key,label in [('task_id','任务'),('device_sn','EGO'),('ssid','Wi-Fi')] if target.get(key))
    waiting_html = f'<aside class="waiting"><strong>需要人工操作</strong><p>{esc(waiting.get("message", ""))}</p><p>完成后回到工作台点击“已完成，继续”。</p></aside>' if waiting else ''
    scope = ('全量自动巡检包含 3 项框架检查和 9 项设备流程。未勾选的参数修改、录制及上传会记为未测。'
             '游客登录 5 项、当前预览独立检查 3 项需要不同前置条件，请在工作台对应模式另行执行；65 项完整人工清单也不计入本轮自动结果。') if session.get('mode') == 'flow_full' else (
             '本报告只统计本轮检查，不继承历史通过结果。未测、未执行、待确认均不计为通过。')
    comparison = ''
    if (folder/'recording_comparison.json').is_file():
        import json
        try:
            result = json.loads((folder/'recording_comparison.json').read_text(encoding='utf-8'))
            items = [('传感器消息与时间戳未改变', result.get('messages_unchanged')),
                     ('写入的元数据与源文件一致', result.get('source_content_matches'))]
            comparison = '<h3>文件校验</h3>' + ''.join(f'<p><span class="badge {"pass" if value is True else "review"}">{"通过" if value is True else "需复核"}</span> {label}</p>' for label,value in items)
        except (OSError, ValueError, TypeError, AttributeError):
            comparison = '<p>文件对比结果无法读取，请查看原始记录。</p>'
    return TEMPLATE.replace('<!--CONTENT-->', f'''
<header><div class="eyebrow">EGO · APP TEST WORKBENCH</div><h1>APP 巡检报告</h1><p>{esc(mode_title)}</p>
<div class="meta"><span>执行时间 {esc(session.get('created',''))}</span><span>手机 {esc(session.get('serial') or '本轮不操作手机')}</span>{target_html}<span>记录 {esc(session.get('id',''))}</span></div></header>
<main><nav><a href="#overview">结果概览</a><a href="#issues">问题与待确认</a><a href="#checks">检查列表</a><a href="#evidence">截图证据</a><button onclick="window.print()">打印 / 保存 PDF</button></nav>
<section id="overview"><div class="section-title"><h2>结果概览</h2><span class="badge {tone}">{esc(state)}</span></div>
<div class="conclusion {tone}">{conclusion}</div>{waiting_html}<div class="metrics">{cards}</div>
<p class="muted">通过占比 {passed}/{total}（{round(passed/total*100) if total else 0}%）。统计范围为本轮已列出的检查，不代表全部 APP 功能覆盖率。</p></section>
<section id="issues"><h2>问题与待确认</h2>{issues_html}{comparison}</section>
<section id="checks"><div class="section-title"><h2>检查列表</h2><span id="visible-count">共 {total} 项</span></div>
<div class="filters"><label>结果 <select id="status"><option value="">全部结果</option>{''.join(f'<option>{s}</option>' for s in STATUS)}</select></label><label>搜索 <input id="search" placeholder="检查项、模块或记录" type="search"></label></div>
<div class="table-wrap"><table><thead><tr><th>结果</th><th>检查项目</th><th>说明与记录</th></tr></thead><tbody>{''.join(rows)}</tbody></table></div><p id="empty" hidden>没有符合条件的检查项。</p></section>
<section id="evidence"><h2>截图证据</h2><p class="muted">点击缩略图查看原图。截图按生成顺序展示，具体步骤以文件名和检查记录为准。</p><div class="gallery">{''.join(evidence) or '<p>本轮没有生成截图证据。</p>'}</div></section>
<section><h2>检查范围与下一步</h2><p>{scope}</p><p class="muted">若存在未测项，请根据完整记录补齐登录、设备连接、换卡或操作勾选，再运行相应流程。APP 的上传完成状态不代表云端转换或质检完成；文件时间戳检查不代表传感器物理同步。</p><div class="downloads">{links}</div></section>
<footer>本报告由本轮 session.json 生成 · 历史记录与本轮结果独立保存</footer></main>''')


TEMPLATE = '''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>APP 巡检报告</title>
<style>
:root{color-scheme:light;--ink:#172c44;--muted:#64748b;--line:#e1e8f0;--blue:#265fd6}*{box-sizing:border-box}html{scroll-behavior:smooth}body{margin:0;background:#f2f5fa;color:var(--ink);font:15px/1.7 "Microsoft YaHei",system-ui,sans-serif}header{background:#13283f;color:white;padding:36px max(24px,calc((100vw - 1160px)/2)) 30px}h1{font-size:30px;margin:8px 0}h2{font-size:20px;margin:0 0 18px}h3{font-size:17px}header p{color:#c6d5e7;margin:0}.eyebrow{font-size:11px;letter-spacing:2px;color:#9ebfe9}.meta{display:flex;flex-wrap:wrap;gap:8px 24px;color:#c6d5e7;font-size:12px;margin-top:22px}main{max-width:1208px;margin:auto;padding:0 24px 36px}nav{display:flex;align-items:center;flex-wrap:wrap;gap:10px 24px;padding:20px 0}a{color:var(--blue);text-decoration:none}a:hover{text-decoration:underline}button{font:inherit;cursor:pointer}nav button{margin-left:auto;background:white;border:1px solid var(--line);border-radius:8px;padding:7px 15px;color:var(--ink)}section{background:white;border:1px solid var(--line);border-radius:14px;padding:25px;margin-bottom:20px;scroll-margin-top:16px}.section-title{display:flex;justify-content:space-between;align-items:center;gap:16px}.section-title h2{margin-bottom:16px}.section-title>span{font-size:13px}.conclusion{padding:12px 16px;border-radius:8px;font-size:17px;font-weight:600;margin:0 0 20px}.metrics{display:grid;grid-template-columns:repeat(5,1fr);gap:12px}.metric{border:1px solid var(--line);border-radius:10px;padding:16px;background:#f8fafc}.metric span{display:block;font-size:13px}.metric strong{display:block;font-size:32px;line-height:1.4}.badge{display:inline-block;padding:3px 10px;border-radius:6px;white-space:nowrap;font-size:12px;font-weight:600}.pass{color:#176441;background:#eaf7ef}.fail{color:#ac2938;background:#fff0f1}.review{color:#8b5b13;background:#fff6e3}.skip,.pending{color:#52667d;background:#edf2f7}.muted,small{color:var(--muted)}small{display:block;font-size:12px;overflow-wrap:anywhere}.issue{border:1px solid var(--line);border-radius:9px;padding:15px;margin:10px 0}.issue>a{font-weight:600;margin-left:12px}.issue p{margin:9px 0 0;overflow-wrap:anywhere}.filters{display:flex;flex-wrap:wrap;gap:16px;margin-bottom:16px}.filters label{display:flex;align-items:center;gap:8px}input,select{font:inherit;padding:8px 10px;border:1px solid #c8d4e2;border-radius:7px;background:white;color:var(--ink)}input{min-width:260px}.table-wrap{overflow:auto}table{border-collapse:collapse;width:100%}th{text-align:left;background:#f5f8fc;font-weight:500;color:var(--muted)}td,th{padding:14px 12px;border-bottom:1px solid var(--line);vertical-align:top}td:first-child{width:86px}td:nth-child(2){width:32%}td:last-child{overflow-wrap:anywhere}details{margin-top:8px;font-size:12px}summary{cursor:pointer;color:var(--blue)}pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f7fa;border-radius:6px;padding:12px;max-height:300px;overflow:auto}code{overflow-wrap:anywhere}tr:target{outline:2px solid #78a7f5;outline-offset:-2px}.gallery{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px}figure{margin:0;border:1px solid var(--line);border-radius:9px;overflow:hidden}figure img{width:100%;height:190px;object-fit:contain;background:#eaf0f7;display:block}figcaption{padding:12px}figcaption a{font-size:12px}.downloads{display:flex;gap:12px;flex-wrap:wrap}.downloads a{background:#edf3ff;padding:8px 12px;border-radius:7px}footer{font-size:12px;text-align:center;color:var(--muted)}.waiting{border:1px solid #e9bd63;padding:14px;border-radius:8px;margin-bottom:18px;background:#fff9ec}.waiting p{margin:4px 0}[hidden]{display:none!important}
@media(max-width:700px){header{padding:24px}main{padding:0 12px 24px}section{padding:18px}.metrics{grid-template-columns:repeat(2,1fr)}nav{gap:10px;font-size:13px}nav button{margin-left:0}.filters label,input{width:100%;min-width:0}.gallery{grid-template-columns:1fr}td:nth-child(2){min-width:160px}td:last-child{min-width:240px}}
@media print{body{background:white;font-size:11px}header{background:white;color:#172c44;padding:10px 0}header p,.eyebrow,.meta{color:#475569}nav,.filters,.downloads{display:none}main{max-width:none;padding:0}section{break-inside:auto;padding:12px;margin:12px 0}tr,figure,.issue{break-inside:avoid}tr[hidden]{display:table-row!important}details pre{max-height:none}figure img{height:150px}.metrics{grid-template-columns:repeat(5,1fr)}a{color:inherit}h1{font-size:24px}}
</style></head><body><!--CONTENT--><script>
const rows=Array.from(document.querySelectorAll('tbody tr'));function filter(){const s=document.getElementById('status').value;const q=document.getElementById('search').value.trim().toLocaleLowerCase();let n=0;for(const r of rows){r.hidden=!!((s&&r.dataset.status!==s)||(q&&!r.textContent.toLocaleLowerCase().includes(q)));if(!r.hidden)n++;}document.getElementById('visible-count').textContent='显示 '+n+' / '+rows.length+' 项';document.getElementById('empty').hidden=n!==0;}
document.getElementById('status').addEventListener('change',filter);document.getElementById('search').addEventListener('input',filter);document.querySelectorAll('.issue a').forEach(a=>a.addEventListener('click',()=>{document.getElementById('status').value='';document.getElementById('search').value='';filter();}));
</script></body></html>'''
