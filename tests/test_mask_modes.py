import sys
sys.path.insert(0, 'e:/vscodeProject/vedioDealProject')
import importlib.util

def load_from_str(name, code):
    spec = importlib.util.spec_from_loader(name, loader=None)
    m = importlib.util.module_from_spec(spec)
    exec(code, m.__dict__)
    return m

raw = open('app/core/ffmpeg_proc.py', encoding='utf-8').read()
for key in ['from app.config import settings',
            'from app.core.sandbox import SandboxError, sandbox',
            'from __future__ import annotations']:
    raw = raw.replace(key, '')
m = load_from_str('ffmpeg_proc', raw)
VideoInfo = m.VideoInfo
bpf = m.build_process_filter
_normalize_mask = m._normalize_mask
_safe_boxblur = m._safe_boxblur
_solid_cover = m._solid_cover

info = VideoInfo(path='t.mp4', width=1920, height=1080, duration=60.0, fps=30.0, codec='h264')

# ---------- 1. boxblur 参数名验证（不能用 luma_r= 那种长名不匹配的形式）----------
bb = _safe_boxblur(200, 100, 'medium')
print('CASE A boxblur medium=', repr(bb))
assert 'lr=' in bb and 'lp=' in bb, 'boxblur should use short names lr/lp'
assert 'luma_r=' not in bb, 'boxblur MUST NOT use luma_r= (option not found error)'
print('  OK: 使用短名 lr/lp/cr/cp/ar/ap ✔')

# ---------- 2. _normalize_mask 支持 4/5/6-tuple/dict ----------
cases = [
    # 4-tuple → 默认 blur+medium
    ((10, 20, 100, 80),                          {'x':10,'y':20,'w':100,'h':80,'mode':'blur','strength':'medium'}),
    # 6-tuple (cover)
    ((10, 20, 100, 80, 'cover', 'strong'),        {'x':10,'y':20,'w':100,'h':80,'mode':'cover','strength':'strong'}),
    # dict 格式（前端提交）
    ({'x':5,'y':15,'w':120,'h':60,'mode':'blur','strength':'light'},
                                                  {'x':5,'y':15,'w':120,'h':60,'mode':'blur','strength':'light'}),
]
for i, (inp, expected) in enumerate(cases):
    got = _normalize_mask(inp)
    for k, v in expected.items():
        assert got[k] == v, f'CASE B{i} key={k} expect={v} got={got[k]}'
    print(f'CASE B{i} normalize_mask OK:', got)

# ---------- 3. filter_complex 中不应再出现 luma_r= ----------
filt, w, h = bpf(info, banner_region=None,
                 logos=[(30, 30, 180, 80)],
                 mask_regions=[(400, 200, 200, 100, 'blur', 'strong'),
                               (700, 300, 250, 120, 'cover', 'medium')])
print('CASE C (1 logo + 2 masks): size', w, 'x', h, 'parts', len(filt.split(';')))
print('  filter (first 400 chars):', filt[:400])
# 禁止任何 luma_r / luma_p 参数写法
assert 'luma_r=' not in filt and 'luma_p=' not in filt, '禁止 luma_r/luma_p 旧写法（Option not found）'
assert 'lr=' in filt, '应使用 lr= 短名格式'
assert 'drawbox=' in filt and 't=fill' in filt, 'cover 模式应包含 drawbox 填充'
print('  OK: logo 使用 boxblur lr= 短名；cover 蒙版用 drawbox 纯色填充 ✔')

# ---------- 4. 三档 strength 对应的 lr 值差异 ----------
bl = _safe_boxblur(300, 150, 'light')
bm = _safe_boxblur(300, 150, 'medium')
bs = _safe_boxblur(300, 150, 'strong')
import re
def get_lr(s): return int(re.search(r'lr=(\d+)', s).group(1))
rl, rm, rs = get_lr(bl), get_lr(bm), get_lr(bs)
print(f'CASE D strength → lr: light={rl}, medium={rm}, strong={rs}')
assert rl <= rm <= rs and rs > rl, '强度等级必须严格递增：light < medium < strong'
print('  OK: 三档强度差异明确 ✔')

print('\n======== ALL TESTS PASSED ========')
