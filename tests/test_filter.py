import sys
sys.path.insert(0, 'e:/vscodeProject/vedioDealProject')
# 只测 build_process_filter 本身（不需要第三方）
import importlib.util

def load_from_str(name, code):
    spec = importlib.util.spec_from_loader(name, loader=None)
    m = importlib.util.module_from_spec(spec)
    exec(code, m.__dict__)
    return m

raw = open('app/core/ffmpeg_proc.py', encoding='utf-8').read()
# 移除依赖外部 import
for key in ['from app.config import settings',
            'from app.core.sandbox import SandboxError, sandbox',
            'from __future__ import annotations']:
    raw = raw.replace(key, '')
m = load_from_str('ffmpeg_proc', raw)
VideoInfo = m.VideoInfo
bpf = m.build_process_filter

info = VideoInfo(path='t.mp4', width=1920, height=1080, duration=60.0, fps=30.0, codec='h264')

filt, w, h = bpf(info, banner_region=(0, 0, 1920, 120),
                 logos=[(30, 30, 180, 80), (1600, 40, 280, 60)])
print('CASE 1 (top banner + 2 logos): out_w/h =', w, h, ' prefinal=', '[prefinal]' in filt)
print('  filter parts:', len(filt.split(';')))

filt2, w2, h2 = bpf(info, banner_region=None, logos=None)
# 这里会走 need_filter=False 的路径，本函数仅返回 ("copy",...) 是主调用方的简化，但我们的函数要求
# banner/logo 都为空时也会构造 chain，最后加 copy[prefinal]
print('CASE 2 (nothing): out_w/h=', w2, h2, ' prefinal=', '[prefinal]' in filt2)

filt3, w3, h3 = bpf(info, banner_region=(0, 980, 1920, 100), logos=[])
print('CASE 3 (bottom banner): w/h=', w3, h3, ' prefinal=', '[prefinal]' in filt3)

# 验证输出：banner 为 (0,0,1920,120) 顶部横幅时，保留部分从 120 开始 overlay_y=120，最终输出 1920x1080
assert w == 1920 and h == 1080, f'CASE1 size wrong {w}x{h}'
assert w3 == 1920 and h3 == 1080, f'CASE3 size wrong {w3}x{h3}'
print('ALL_OK')
