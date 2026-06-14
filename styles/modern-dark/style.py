"""现代暗色风格背景：深色底色 + 顶部微亮渐变，Linear/Stripe 设计风格"""
from PIL import Image, ImageDraw


def dark_bg(width: int, height: int) -> Image.Image:
    """深色背景，顶部叠加微弱亮色渐变增强页面层次感"""
    base = Image.new("RGBA", (width, height), (18, 20, 24, 255))

    # 顶部区域叠加极微弱的亮色渐变（仅 top 15%，最多 120px），营造纵深
    overlay = Image.new("RGBA", (width, height))
    draw = ImageDraw.Draw(overlay)
    gradient_height = min(int(height * 0.15), 120)

    for y in range(gradient_height):
        alpha = int(10 * (1 - y / gradient_height))
        if alpha > 0:
            draw.line([(0, y), (width, y)], fill=(50, 55, 62, alpha))

    return Image.alpha_composite(base, overlay)
