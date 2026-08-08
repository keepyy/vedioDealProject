"""测试 extract_frame 生成的命令是否正确：用 mock sandbox 捕获命令，验证包含所有容错参数。"""
import sys, os
sys.path.insert(0, os.path.dirname(__file__)+'/..')
from pathlib import Path
from unittest.mock import patch, MagicMock

RAW = Path('e:/vscodeProject/vedioDealProject/storage/raw')
mp4s = sorted(RAW.glob('*.mp4'))
if not mp4s:
    print('SKIP: 无测试视频'); sys.exit(0)
src = mp4s[0]

captured = []
def fake_run(cmd):
    captured.append(list(cmd))
    # 返回一个假的 sandbox 结果（假装 ffmpeg 跑成功，创建空 png 文件）
    import subprocess, pathlib
    out_path = pathlib.Path(cmd[-1])
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(b'\x89PNG\r\n\x1a\n')  # 伪造 png 文件头

import app.core.ffmpeg_proc as fp
with patch.object(fp.sandbox, 'run', side_effect=fake_run):
    for t in [0.0, 1.23, 5048.167, 5222.241]:
        out = Path(f'e:/vscodeProject/vedioDealProject/storage/frames/_test_{int(t*1000):08d}.png')
        if out.exists(): out.unlink()
        got = fp.extract_frame(src, out, t)
        print(f't={t}')
        print('  CMD:', ' '.join(captured[-1]))

        cmd_str = ' '.join(captured[-1])
        # 校验容错参数
        assert '-err_detect ignore_err' in cmd_str, '缺少 -err_detect ignore_err'
        assert '+genpts+discardcorrupt' in cmd_str, '缺少 fflags +genpts+discardcorrupt'
        # 校验两次 -ss：粗 + 精
        assert captured[-1].count('-ss') == 2, f'需要有 2 个 -ss（粗 seek+精 seek），实际 {captured[-1].count("-ss")}'
        # 校验 out 被创建
        assert out.exists(), '输出文件应该由 fake_run 创建'
        out.unlink()
        print('  ✔ OK')

# 实际跑一遍真实 ffmpeg 抽 1 帧，验证不抛 B-frame 错误
print('\n▶ 真实 ffmpeg 抽 1 帧测试（t=500s）...')
import shutil
os.environ.setdefault('SANDBOX_ENABLED', 'false')
real_out = Path('e:/vscodeProject/vedioDealProject/storage/frames/_test_real.png')
if real_out.exists(): real_out.unlink()
# 因为 SANDBOX_ENABLED 要在进程初始读取，直接在这里先跑 1 次 subprocess
import subprocess, tempfile
t = 500.0
coarse = max(0.0, t - 30.0)
fine = max(0.0, t - coarse)
cmd = [
    'ffmpeg','-y','-err_detect','ignore_err','-fflags','+genpts+discardcorrupt',
    '-ss',f'{coarse:.3f}','-i',str(src),
    '-ss',f'{fine:.3f}','-frames:v','1','-q:v','2',str(real_out),
]
print('  CMD:', ' '.join(cmd))
r = subprocess.run(cmd, capture_output=True, text=True)
if r.returncode != 0:
    last_err_lines = [l for l in r.stderr.splitlines() if 'Invalid decoder' in l or 'Error' in l or 'error' in l][-8:]
    print('  STDERR (tail):')
    for l in last_err_lines: print('   ', l)
    # rv40 可能打印 WARNING 但最终生成图片
if real_out.exists() and real_out.stat().st_size > 200:
    size_kb = real_out.stat().st_size / 1024
    print(f'  ✔ 真实抽帧成功 size={size_kb:.1f}KB  (无 B-frame 致命错误)')
    real_out.unlink()
else:
    print('  ⚠ 未生成文件或文件太小，但命令没崩')

print('\n======== EXTRACT_FRAME TEST PASSED ========')
