"""端到端测试：直接调用 cut_and_process_segment，用同一个输入视频跑两种蒙版模式，验证：
1) boxblur luma_r → lr 短名修复；
2) 两种模式（blur+cover）都能正确输出视频文件；
3) 滤镜强度差异体现在生成命令中（light lr<=medium lr<=strong lr）。"""
import sys, os, json, subprocess, tempfile, shutil
sys.path.insert(0, os.path.dirname(__file__) + '/..')
from pathlib import Path
from app.core.ffmpeg_proc import (
    cut_and_process_segment, _safe_boxblur,
    build_process_filter, VideoInfo, probe_video as ffmpeg_probe
)

RAW = Path('e:/vscodeProject/vedioDealProject/storage/raw')
OUT = Path('e:/vscodeProject/vedioDealProject/storage/final/_test_mask_modes')
OUT.mkdir(parents=True, exist_ok=True)

# 找一个存在的 mp4
candidates = sorted(RAW.glob('*.mp4'))
if not candidates:
    print('SKIP: storage/raw 无 mp4 测试文件'); sys.exit(0)
src = candidates[0]
print('测试视频：', src.name)
info = ffmpeg_probe(str(src))
print('  info:', info)

# ---------- 1. 强度三档命令差异 ----------
rl = _safe_boxblur(200, 120, 'light')
rm = _safe_boxblur(200, 120, 'medium')
rs = _safe_boxblur(200, 120, 'strong')
import re
def L(s): return int(re.search(r'lr=(\d+)', s).group(1))
assert L(rl) < L(rm) < L(rs), f'强度必须递增：light={L(rl)} medium={L(rm)} strong={L(rs)}'
print(f'\n[OK] 三档 boxblur lr 差异：light={L(rl)} / medium={L(rm)} / strong={L(rs)}')
assert 'luma_r=' not in (rl+rm+rs), '禁止 luma_r= 写法'

# ---------- 2. 构造 filter_complex 预览（blur+cover 各一个蒙版）----------
filt, W, H = build_process_filter(
    info,
    banner_region=None,
    logos=[(50, 50, 150, 60)],
    mask_regions=[
        (200, 150, 250, 120, 'blur', 'strong'),
        (500, 300, 300, 150, 'cover', 'medium'),
    ],
)
print('\n[OK] filter_complex 生成（含 blur+cover 双模式）：')
print('   ', filt[:500], '...' if len(filt) > 500 else '')
assert 'drawbox=' in filt and 't=fill' in filt, 'cover 模式必须是 drawbox 填色'
assert 'luma_r=' not in filt, '绝对不能再出现 luma_r=（Option not found）'

# ---------- 3. 实际运行 ffmpeg：生成带蒙版的片段 ----------
test_cases = [
    ('blur_light',   [(200, 150, 250, 120, 'blur',  'light')]),
    ('blur_medium',  [(200, 150, 250, 120, 'blur',  'medium')]),
    ('blur_strong',  [(200, 150, 250, 120, 'blur',  'strong')]),
    ('cover_mode',   [(500, 300, 300, 150, 'cover', 'medium')]),
    ('mixed',        [(200, 150, 250, 120, 'blur','strong'), (500,300,300,150,'cover','medium')]),
]
all_ok = True
for tag, masks in test_cases:
    out = OUT / f'{src.stem}__{tag}.mp4'
    if out.exists(): out.unlink()
    print(f'\n▶ 运行 {tag} → {out.name} ...')
    try:
        end_sec = min(info.duration, 5.0)
        cut_and_process_segment(
            video_path=Path(src),
            start_sec=0.0, end_sec=end_sec,
            output_path=Path(out),
            banner_region=None,
            logos=[],
            mask_regions=masks,
        )
        sz = out.stat().st_size
        dur_ok = True
        try:
            probe_out = ffmpeg_probe(str(out))
            print(f'    ✔ 输出 OK size={sz//1024}KB duration={probe_out.duration:.2f}s (was {end_sec:.1f}s target)')
        except Exception as e:
            print(f'    ⚠ ffprobe失败：{e}（文件size={sz}）')
            dur_ok = False
        if sz < 50_000 or not dur_ok:
            print(f'    ✗ FAIL 输出异常（size<50KB 或 duration 不对）')
            all_ok = False
    except Exception as e:
        print(f'    ✗ 执行失败：{type(e).__name__}: {e}')
        all_ok = False

print('\n========= 测试结果 =========')
if all_ok:
    print('✅ 全部通过！蒙版强度三档差异明显，blur+cover 双模式均能正确输出。')
else:
    print('❌ 有失败案例，请查看日志。')
    sys.exit(1)
