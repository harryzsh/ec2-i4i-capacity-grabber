#!/usr/bin/env python3
"""画 --watch 两层循环机制图。中文用文泉驿正黑。"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
from matplotlib.font_manager import FontProperties

FONT = FontProperties(fname="/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc")

def cn(ax, x, y, s, size=12, color="#1a1a1a", weight="normal", ha="center", va="center"):
    ax.text(x, y, s, fontproperties=FONT, fontsize=size, color=color,
            ha=ha, va=va, fontweight=weight, zorder=5)

def box(ax, x, y, w, h, fc, ec, lw=1.5, r=0.06):
    p = FancyBboxPatch((x-w/2, y-h/2), w, h,
                       boxstyle=f"round,pad=0.02,rounding_size={r}",
                       fc=fc, ec=ec, lw=lw, zorder=3)
    ax.add_patch(p)

def arrow(ax, x1, y1, x2, y2, color="#444", lw=2, style="-|>", rad=0.0):
    a = FancyArrowPatch((x1, y1), (x2, y2), arrowstyle=style,
                        mutation_scale=18, color=color, lw=lw,
                        connectionstyle=f"arc3,rad={rad}", zorder=4)
    ax.add_patch(a)

fig, ax = plt.subplots(figsize=(9, 11))
ax.set_xlim(0, 10); ax.set_ylim(0, 13); ax.axis("off")

# 颜色
BLUE="#2563eb"; BLUE_BG="#eff6ff"
GREEN="#16a34a"; GREEN_BG="#f0fdf4"
ORANGE="#ea580c"; ORANGE_BG="#fff7ed"
GRAY="#64748b"; GRAY_BG="#f8fafc"
RED="#dc2626"

# 标题
cn(ax, 5, 12.4, "--watch  24×7 死等模式  =  两层循环", size=17, weight="bold", color="#0f172a")
cn(ax, 5, 11.85, "（不开 --watch：只跑下面虚线框一遍就退出）", size=10.5, color=GRAY)

# 启动
box(ax, 5, 11.0, 7.6, 0.62, GRAY_BG, GRAY)
cn(ax, 5, 11.0, "启动：grab_ondemand.py --watch --interval 30 --target-cores 10000", size=10.5, weight="bold")

arrow(ax, 5, 10.69, 5, 10.25)

# ===== 第一层外框 =====
box(ax, 5, 7.7, 8.2, 4.7, BLUE_BG, BLUE, lw=2)
cn(ax, 5, 9.75, "第 1 层 · 一轮扫描  sweep_once()", size=13.5, weight="bold", color=BLUE)
cn(ax, 5, 9.32, "连续不停地打，一轮内【不睡觉】", size=10.5, color=BLUE)

# 机型 × 可用区 网格说明
box(ax, 5, 8.45, 7.2, 0.95, "#ffffff", BLUE, lw=1.2)
cn(ax, 5, 8.72, "按机型从大到小  ×  逐个可用区  连着试：", size=10.5, weight="bold")
cn(ax, 5, 8.22, "8xlarge → 4xlarge → 2xlarge → xlarge → large", size=10, color="#334155")

# AZ 行
azs = ["us-east-1a","1b","1c","1d"]
xs = [2.4, 4.05, 5.7, 7.35]
for x, az in zip(xs, azs):
    box(ax, x, 7.15, 1.45, 0.55, "#ffffff", "#93c5fd", lw=1)
    cn(ax, x, 7.15, az, size=9.5, color="#1e3a8a")
cn(ax, 5, 6.55, "抢到一台 → 记 logs/grabs.jsonl，累加 vCPU", size=10, color="#334155")

arrow(ax, 5, 5.33, 5, 4.95)

# ===== 判断菱形（用方框代替）=====
box(ax, 5, 4.55, 5.6, 0.75, ORANGE_BG, ORANGE, lw=1.8)
cn(ax, 5, 4.55, "累计够 --target-cores（10000 vCPU）了吗？", size=11, weight="bold", color=ORANGE)

# 是 → 退出
arrow(ax, 7.8, 4.55, 9.0, 4.55, color=GREEN)
box(ax, 9.0, 4.55, 1.7, 0.7, GREEN_BG, GREEN, lw=1.8)
cn(ax, 9.0, 4.7, "够了", size=10.5, weight="bold", color=GREEN)
cn(ax, 9.0, 4.35, "停止退出", size=9.5, color=GREEN)

# 否 → 第二层
arrow(ax, 5, 4.17, 5, 3.7, color=RED)
cn(ax, 5.35, 3.95, "不够", size=10, color=RED, weight="bold", ha="left")

# ===== 第二层 =====
box(ax, 5, 3.25, 6.2, 0.85, ORANGE_BG, ORANGE, lw=2)
cn(ax, 5, 3.45, "第 2 层 · 睡 --interval 秒（默认 60，这里 30）", size=11.5, weight="bold", color=ORANGE)
cn(ax, 5, 3.0, "歇一下，避免把 AWS API 打到限流", size=10, color="#9a3412")

# 回流箭头：从第二层左侧绕回第一层
arrow(ax, 1.9, 3.25, 0.75, 3.25, color=ORANGE, lw=2)
arrow(ax, 0.75, 3.25, 0.75, 7.7, color=ORANGE, lw=2, style="-")
arrow(ax, 0.75, 7.7, 0.9, 7.7, color=ORANGE, lw=2)
cn(ax, 0.5, 5.5, "无限循环", size=11, color=ORANGE, weight="bold")
cn(ax, 0.95, 5.05, "回到第 1 层\n再扫一轮", size=9, color=ORANGE)

# 底部小结
box(ax, 5, 1.55, 8.6, 1.5, GREEN_BG, GREEN, lw=1.5)
cn(ax, 5, 2.18, "一句话总结", size=11, weight="bold", color=GREEN)
cn(ax, 5, 1.7,
   "产能是 AWS 间歇性放出来的，单次扫大概率空手。", size=10.2, color="#14532d")
cn(ax, 5, 1.28,
   "--watch 就是「死等」：每 30 秒重扫一轮，直到凑够目标，盯着才抢得到。", size=10.2, color="#14532d")

plt.tight_layout()
out = "/home/ubuntu/i4i-grab/docs/watch-diagram.png"
plt.savefig(out, dpi=150, bbox_inches="tight", facecolor="white")
print("saved:", out)
