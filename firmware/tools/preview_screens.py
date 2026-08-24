# 复刻 ui_render.c 的布局逻辑,验证 250×122 下中文屏不越界
import argparse
import bisect
import struct
from pathlib import Path
from PIL import Image

PROJECT = Path(__file__).resolve().parent.parent
W, H = 250, 122

parser = argparse.ArgumentParser()
parser.add_argument("--font", type=Path, default=PROJECT / "assets" / "font16.bin")
parser.add_argument("--out", type=Path, default=PROJECT / "screens_preview.png")
args = parser.parse_args()

blob = args.font.read_bytes()
assert blob[:4] == b"CMF1"
count = struct.unpack_from("<I", blob, 8)[0]
cps = list(struct.unpack_from(f"<{count}H", blob, 16))
gbase = 16 + count * 2

img = None
px = None

def new_screen():
    global img, px
    img = Image.new("1", (W, H), 1)   # 1=白
    px = img.load()

def rect(x, y, w, h):
    for j in range(y, y + h):
        for i in range(x, x + w):
            if 0 <= i < W and 0 <= j < H:
                px[i, j] = 0

def glyph(cp):
    i = bisect.bisect_left(cps, cp)
    if i < len(cps) and cps[i] == cp:
        return blob[gbase + i * 32: gbase + (i + 1) * 32]
    return None

def adv(cp, sc): return (8 if cp < 0x100 else 16) * sc

def utext(x, y, sc, s):
    for ch in s:
        g = glyph(ord(ch))
        if g:
            for row in range(16):
                bits = (g[row * 2] << 8) | g[row * 2 + 1]
                for col in range(16):
                    if bits & (0x8000 >> col):
                        rect(x + col * sc, y + row * sc, sc, sc)
        elif ch != ' ':
            rect(x + sc, y + sc, 14 * sc, sc); rect(x + sc, y + 14 * sc, 14 * sc, sc)
            rect(x + sc, y + sc, sc, 14 * sc); rect(x + 14 * sc, y + sc, sc, 14 * sc)
        x += adv(ord(ch), sc)
    return x

def utext_w(s, sc): return sum(adv(ord(c), sc) for c in s)
def ucenter(y, sc, s): utext((W - utext_w(s, sc)) // 2, y, sc, s)
def uright(xr, y, sc, s): utext(xr - utext_w(s, sc), y, sc, s)
def top_bar(l, c, r):
    if l: utext(2, 1, 1, l)
    if c: ucenter(1, 1, c)
    if r: uright(248, 1, 1, r)
    rect(0, 18, 250, 1)
def bottom_bar(l, r):
    rect(0, 103, 250, 1)
    if l: utext(2, 105, 1, l)
    if r: uright(248, 105, 1, r)

def wrap_text(x, y, max_w, max_lines, s):
    cx, line = x, 0
    for ch in s:
        a = adv(ord(ch), 1)
        if cx + a > x + max_w:
            cx = x; line += 1
            if line >= max_lines: break
        utext(cx, y + line * 18, 1, ch)
        cx += a

shots = []

# 待机页
new_screen()
top_bar("WiFi", "待机", "82%")
ucenter(20, 3, "14:23")
ucenter(68, 1, "7月10日 周四")
ucenter(86, 1, "下一项 15:00 产品评审")
bottom_bar("[会议]", "MARK切  REC录")
shots.append(("standby", img))

# 今日议程页
new_screen()
top_bar("日程", "今日议程", "82%")
utext(6, 22, 1, "15:00  产品评审")
utext(6, 41, 1, "17:30  项目周会")
utext(6, 60, 1, "20:00  客户回访")
bottom_bar(None, "BACK下一页")
shots.append(("agenda", img))

# 设备状态页（版本字符串必须完整落在 250px 内）
new_screen()
top_bar("设备", "状态", "82%")
utext(10, 22, 1, "网络 在线  云端 可用")
utext(10, 41, 1, "账号 已绑定  积压 0秒")
utext(10, 60, 1, "SD    正常  充电 未充")
utext(10, 79, 1, "固件  0.6.1")
bottom_bar(None, "BACK返回  长按配网")
shots.append(("status", img))

# 录音字幕页
new_screen()
top_bar("REC 12:34", "会议", "UP0 82%")
wrap_text(2, 22, 246, 4, "先把采集和上传跑通,纪要放在云端生成,断网也照常写入SD卡,联网后自动补传补转写,这样一个字都不会丢。")
bottom_bar("重点×2  待办×1", "会议")
shots.append(("recording", img))

# 翻译大字页
new_screen()
top_bar("REC 02:18", "翻译", "UP0 82%")
wrap_text(2, 21, 246, 1, "Please review chapter five before Friday.")
rect(0, 39, 250, 1)
wrap_text(2, 43, 246, 3, "请在周五前复习第五章。")
bottom_bar("收藏×1  重点×0", "译文")
shots.append(("translation", img))

# 双语对照页
new_screen()
top_bar("REC 02:18", "翻译", "UP0 82%")
wrap_text(2, 21, 246, 2, "Please review chapter five before Friday.")
rect(0, 58, 250, 1)
wrap_text(2, 62, 246, 2, "请在周五前复习第五章。")
bottom_bar("收藏×1  重点×0", "双语")
shots.append(("bilingual", img))

# 充电页
new_screen()
top_bar("WiFi", "充电", "82%")
ucenter(25, 1, "USB-C 已接入")
ucenter(46, 2, "82%")
ucenter(81, 1, "REC开始  BACK状态")
bottom_bar("[会议]", "MARK切场景")
shots.append(("charging", img))

# 到点提醒
new_screen()
top_bar(None, "到点提醒", None)
wrap_text(6, 25, 238, 3, "明晚七点学生会与项目复盘")
bottom_bar("REC录  MARK延10分", "BACK关闭")
shots.append(("reminder", img))

# 待办确认
new_screen()
top_bar(None, "确认待办", None)
wrap_text(6, 24, 238, 2, "学生会与项目复盘")
ucenter(67, 1, "明天 19:00")
bottom_bar("REC确认", "BACK取消")
shots.append(("todo_confirm", img))

# 关机确认 overlay
new_screen()
top_bar(None, "电源", None)
ucenter(28, 2, "关机?")
ucenter(64, 1, "REC确认  BACK取消")
shots.append(("power", img))

# 拼一张大图(每屏 ×3 放大,竖排)
SC = 3
canvas = Image.new("L", (W * SC + 20, (H * SC + 14) * len(shots) + 10), 200)
for idx, (name, im) in enumerate(shots):
    big = im.resize((W * SC, H * SC), Image.NEAREST).convert("L")
    canvas.paste(big, (10, 10 + idx * (H * SC + 14)))
args.out.parent.mkdir(parents=True, exist_ok=True)
canvas.save(args.out)
print("saved", args.out)
