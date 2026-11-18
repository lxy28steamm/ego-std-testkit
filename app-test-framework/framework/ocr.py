"""OCR fallback for uiautomator dump failures.

Flutter pages intermittently fail `uiautomator dump` ("could not get idle state",
shell "Killed" from OOM, stale uidump lock). When that happens we still need a
usable verdict instead of a hard failure. Recognised text is turned into a
synthetic `hierarchy` tree so the rest of the framework (classify/find/tap)
keeps working unchanged.

OCR engines are optional. Configure with env vars:
  APP_PATROL_OCR=auto|off|windows|tesseract
  APP_PATROL_TESSERACT=<path to tesseract.exe>
  APP_PATROL_OCR_LANG=chi_sim+eng (tesseract only)
"""
from __future__ import annotations
import io
import os
import re
import subprocess
import tempfile
from pathlib import Path
import xml.etree.ElementTree as ET

_WORD_RE = re.compile(r'\S+')
_ENGINE = None
_ENGINE_LOCKED = False


def _mode():
    return os.environ.get('APP_PATROL_OCR', 'auto').strip().lower()


def _decode(raw):
    """PowerShell 控制台输出按系统代码页编码，先按 GBK 解，失败再退回 UTF-8。"""
    if not raw:
        return ''
    for encoding in ('gbk', 'utf-8'):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode('utf-8', 'replace')


def _windows_ocr(image):
    """Windows.Media.Ocr via PowerShell. Chinese/English packs ship with Win10."""
    folder = Path(tempfile.mkdtemp(prefix='app-patrol-ocr-'))
    try:
        source = folder/'shot.png'
        image.save(source)
        script = folder/'ocr.ps1'
        script.write_text(_PS_SCRIPT.replace('%IMAGE%', str(source)), encoding='utf-8')
        out = subprocess.run(['powershell', '-NoProfile', '-ExecutionPolicy', 'Bypass',
                              '-File', str(script)],
                             capture_output=True, timeout=60)
        # Windows PowerShell 的 stdout 是控制台代码页（中文系统为 GBK），
        # 用 UTF-8 解会把中文全变成替换符，页面文字就白读了。
        stdout = _decode(out.stdout)
        stderr = _decode(out.stderr)
        if 'OCR_ERROR' in stdout:
            raise RuntimeError(stdout.strip()[-400:])
        if out.returncode != 0:
            raise RuntimeError((stdout + ' ' + stderr)[-400:].strip())
        words = []
        for line in stdout.splitlines():
            parts = line.strip().split('\t')
            if len(parts) != 5:
                continue
            try:
                x, y, w, h = (int(float(v)) for v in parts[:4])
            except ValueError:
                continue
            if w <= 0 or h <= 0:
                continue
            words.append((parts[4], x, y, x + w, y + h))
        if not words:
            # 引擎可用但一张字都没有：通常是全屏图片/纯色画面，属正常空结果。
            event = ''.join(l for l in stdout.splitlines() if l.startswith('OCR_ENGINE'))
            if not event:
                raise RuntimeError('OCR 无输出：' + stdout[-300:])
        return words
    finally:
        import shutil
        shutil.rmtree(folder, ignore_errors=True)


_PS_SCRIPT = r'''
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
$asTaskAction = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
  $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncAction' })[0]
function Await($task, $resultType) {
  $asTask = $asTaskGeneric.MakeGenericMethod($resultType)
  $t = $asTask.Invoke($null, @($task))
  while (-not $t.IsCompleted) { Start-Sleep -Milliseconds 20 }
  if ($t.IsFaulted) { throw $t.Exception.GetBaseException().Message }
  $t.Result
}
function AwaitAction($action) {
  $t = $asTaskAction.Invoke($null, @($action))
  $t.Wait(-1) | Out-Null
}
[Windows.Media.Ocr.OcrEngine, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
[Windows.Globalization.Language, Windows.Foundation, ContentType=WindowsRuntime] | Out-Null
try {
$path = "%IMAGE%"
$file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($path)) ([Windows.Storage.StorageFile])
$stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { $engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromLanguage([Windows.Globalization.Language]::new("zh-Hans-CN")) }
if ($null -eq $engine) { Write-Output "OCR_ERROR no language pack"; exit 2 }
Write-Output ("OCR_ENGINE " + $engine.RecognizerLanguage.LanguageTag)
$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
foreach ($line in $result.Lines) {
  foreach ($word in $line.Words) {
    $r = $word.BoundingRect
    "{0}`t{1}`t{2}`t{3}`t{4}" -f ([int]$r.X), ([int]$r.Y), ([int]$r.Width), ([int]$r.Height), $word.Text
  }
}
} catch {
  Write-Output ("OCR_ERROR " + $_.Exception.Message)
  exit 3
}
'''


def _tesseract_ocr(image):
    exe = os.environ.get('APP_PATROL_TESSERACT') or 'tesseract'
    lang = os.environ.get('APP_PATROL_OCR_LANG', 'chi_sim+eng')
    with tempfile.TemporaryDirectory() as folder:
        source = Path(folder)/'shot.png'
        image.save(source)
        out = subprocess.run([exe, str(source), 'stdout', '-l', lang, 'tsv'],
                             capture_output=True, timeout=90)
    if out.returncode != 0:
        raise RuntimeError((out.stderr or b'').decode('utf-8', 'replace')[-300:])
    words = []
    for line in out.stdout.decode('utf-8', 'replace').splitlines()[1:]:
        parts = line.split('\t')
        if len(parts) < 12 or not parts[11].strip():
            continue
        try:
            x, y, w, h = (int(parts[i]) for i in (6, 7, 8, 9))
        except ValueError:
            continue
        if w <= 0 or h <= 0:
            continue
        words.append((parts[11].strip(), x, y, x + w, y + h))
    return words


def _pick_engine():
    global _ENGINE, _ENGINE_LOCKED
    if _ENGINE_LOCKED:
        return _ENGINE
    mode = _mode()
    if mode == 'off':
        _ENGINE_LOCKED = True
        return None
    candidates = [('windows', _windows_ocr), ('tesseract', _tesseract_ocr)]
    if mode in ('windows', 'tesseract'):
        candidates = [c for c in candidates if c[0] == mode]
        explicit = True
    else:
        explicit = False
    probe = None
    from PIL import Image, ImageDraw
    probe = Image.new('RGB', (240, 70), 'white')
    ImageDraw.Draw(probe).rectangle([10, 20, 230, 50], fill='black')
    for name, func in candidates:
        try:
            func(probe)
        except Exception:
            continue
        _ENGINE, _ENGINE_LOCKED = func, True
        return _ENGINE
    # auto 模式：引擎暂时不可用不算定论，下次还能再探。
    _ENGINE_LOCKED = explicit
    return None


def recognize(image):
    """Return [(text, x1, y1, x2, y2), ...]; raises if no engine is usable."""
    engine = _pick_engine()
    if engine is None:
        raise RuntimeError('没有可用的 OCR 引擎（APP_PATROL_OCR=%s）' % _mode())
    return engine(image)


def availability():
    """'ready' | 'off' | 'missing' — for surfacing in the run banner."""
    if _mode() == 'off':
        return 'off'
    return 'ready' if _pick_engine() else 'missing'


def build_tree(words):
    """Synthetic hierarchy. Words are grouped into rows so a label like
    '语言\\n简体中文' still matches by substring/contains lookups.

    中文 OCR 常把每个汉字当一个 word，行内间距很小；英文单词间距大。
    所以聚行的容差要大一些，否则每个字会变成独立一行，做不了整句匹配。
    """
    root = ET.Element('hierarchy', {'ocr': 'true'})
    screen = ET.SubElement(root, 'node',
                           {'class': 'android.view.View', 'bounds': '[0,0][9999,9999]'})
    rows = {}
    for text, x1, y1, x2, y2 in words:
        centre = (y1 + y2) // 2
        height = max(1, y2 - y1)
        key = next((k for k in rows if abs(k - centre) <= max(12, height // 2)), centre)
        rows.setdefault(key, []).append((x1, y1, x2, y2, text))
    for key in sorted(rows):
        items = sorted(rows[key], key=lambda w: w[0])
        parts = []
        previous = None
        for x1, y1, x2, y2, text in items:
            # 字间没有空格时直接拼接，避免"开 始 采 集"匹配不到"开始采集"。
            if previous is not None and x1 - previous > max(6, (y2 - y1) // 3):
                parts.append(' ')
            parts.append(text)
            previous = x2
        label = ''.join(parts)
        x1 = min(w[0] for w in items)
        y1 = min(w[1] for w in items)
        x2 = max(w[2] for w in items)
        y2 = max(w[3] for w in items)
        ET.SubElement(screen, 'node', {
            'class': 'android.view.View',
            'content-desc': label,
            'text': label,
            'bounds': '[%d,%d][%d,%d]' % (x1, y1, x2, y2),
            'clickable': 'true',
            'enabled': 'true',
            'ocr': 'true',
        })
    return root


def dump_preview(words, limit=6):
    """Short human-readable sample for the run log, no screenshot kept."""
    return ' / '.join(w[0] for w in words[:limit])
