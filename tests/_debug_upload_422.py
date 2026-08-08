"""POST /upload 422 根因定位：模拟 6 种常见上传场景打印 FastAPI 的 detail。"""
import requests, pathlib, os
RAW = pathlib.Path(r'e:\vscodeProject\vedioDealProject\storage\raw')
jpg = pathlib.Path(r'e:\vscodeProject\vedioDealProject\storage\frames\_test_ref_poster.jpg')

urls = [('local','http://127.0.0.1:8080/upload'),
        ('container','http://localhost:8080/upload')]
for tag, URL in urls:
    print(f'===== 目标: {URL}  =====')
    small_video = None
    for v in RAW.glob('*.mp4'):
        small_video = v
        break
    if not small_video:
        print('无测试视频，跳过'); break

    # 场景 1：完全正确（manual 模式） → 期望 303
    with open(small_video, 'rb') as f:
        files = [('video', (small_video.name, f, 'video/mp4'))]
        data = {'mode': 'manual', 'segment_minutes': '5'}
        r = requests.post(URL, files=files, data=data, allow_redirects=False, timeout=30)
        print(f'S1[OK期望303] → {r.status_code} Location={r.headers.get("Location","")}')
        if r.status_code == 422: print('  detail:', r.json())

    # 场景 2：segment_minutes 传空字符串（用户清空了 number 框） → 大概率 422
    with open(small_video, 'rb') as f:
        files = [('video', (small_video.name, f, 'video/mp4'))]
        data = {'mode': 'manual', 'segment_minutes': ''}
        r = requests.post(URL, files=files, data=data, allow_redirects=False, timeout=30)
        print(f'S2[seg_minutes=空] → {r.status_code}')
        if r.status_code == 422: print('  detail:', r.json())

    # 场景 3：segment_minutes 完全没传 → 期望 OK（默认 0，但因 manual 模式后端会报 400，不是 422）
    with open(small_video, 'rb') as f:
        files = [('video', (small_video.name, f, 'video/mp4'))]
        data = {'mode': 'manual'}
        r = requests.post(URL, files=files, data=data, allow_redirects=False, timeout=30)
        print(f'S3[seg_minutes=未传] → {r.status_code}')
        if r.status_code in (400,422):
            body = r.text[:300] if r.headers.get('content-type','').find('json')<0 else r.json()
            print('  body:', body)

    # 场景 4：视频字段名错 → 422 "video 字段必填"
    with open(small_video, 'rb') as f:
        files = [('wrong_key', (small_video.name, f, 'video/mp4'))]
        data = {'mode': 'manual', 'segment_minutes': '5'}
        r = requests.post(URL, files=files, data=data, allow_redirects=False, timeout=30)
        print(f'S4[video字段名=wrong_key] → {r.status_code}')
        if r.status_code == 422: print('  detail:', r.json())

    # 场景 5：portrait 模式，传视频+人像
    if jpg.exists():
        with open(small_video,'rb') as fv, open(jpg,'rb') as fp:
            files = [('video', (small_video.name, fv, 'video/mp4')),
                     ('portraits', ('a.jpg', fp, 'image/jpeg')),
                     ('portraits', ('a.jpg', fp, 'image/jpeg'))]
            data = {'mode': 'portrait'}
            r = requests.post(URL, files=files, data=data, allow_redirects=False, timeout=30)
            print(f'S5[portrait OK] → {r.status_code} Location={r.headers.get("Location","")}')
            if r.status_code == 422: print('  detail:', r.json())

    # 场景 6：portrait 模式，没传 portraits（绕过前端 required 提交）→ 后端 default=[] 应 OK
    with open(small_video, 'rb') as f:
        files = [('video', (small_video.name, f, 'video/mp4'))]
        data = {'mode': 'portrait'}
        r = requests.post(URL, files=files, data=data, allow_redirects=False, timeout=30)
        print(f'S6[portrait 未传portraits→400非422] → {r.status_code}')
        if r.status_code in (400,422):
            body = r.text[:300] if r.headers.get('content-type','').find('json')<0 else r.json()
            print('  body:', body)
    print()
