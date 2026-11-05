from PIL import Image, ImageOps
import numpy as np

ZOOM_MAX = 1.30   # ซูมเข้าได้มากสุดเท่านี้ (เกินนี้เล็บมือบนจะโดนตัด)
PAD = 0.012   # เว้นระยะจากโลโก้/ป้ายด้านล่าง (สัดส่วนความสูงกรอบ)


def skin_mask(img):
    s = img.convert("YCbCr"); s.thumbnail((300, 300))
    a = np.array(s).astype(int)
    Y, Cb, Cr = a[..., 0], a[..., 1], a[..., 2]
    return (Cr > 135) & (Cr < 180) & (Cb > 85) & (Cb < 135) & (Y > 60)


def overlay_band(frame, box):
    """ช่วงแถวที่ว่างสำหรับเนื้อหา: ใต้โลโก้ด้านบน ถึงเหนือป้ายด้านล่าง (ถ้ามี)"""
    a = np.array(frame.getchannel("A"))
    x0, y0, x1, y1 = box
    rows = np.nonzero((a[y0:y1, x0:x1] >= 1).any(axis=1))[0]
    top, bottom, h = 0, y1 - y0, y1 - y0
    if len(rows):
        segs, s, p = [], rows[0], rows[0]
        for r in rows[1:]:
            if r != p + 1:
                segs.append((s, p)); s = r
            p = r
        segs.append((s, p))
        for s0, e0 in segs:
            if e0 < h * 0.4:
                top = max(top, e0 + 1)
            elif s0 > h * 0.6:
                bottom = min(bottom, s0)
    pad = round(PAD * h)
    return top + pad, bottom - pad


def compose(photo_path, frame_path, out_path, focus=None):
    frame = Image.open(frame_path).convert("RGBA")
    photo = ImageOps.exif_transpose(Image.open(photo_path)).convert("RGB")
    alpha = frame.getchannel("A")
    box = alpha.point(lambda v: 255 if v < 128 else 0).getbbox()
    bw, bh = box[2] - box[0], box[3] - box[1]

    if photo.size == (bw, bh):        # รูปที่จัดตำแหน่งมาแล้ว (ขนาดเท่าช่องกรอบพอดี) ใช้ตามนั้น ไม่ซูม ไม่เลื่อน
        canvas = Image.new("RGBA", frame.size, (255, 255, 255, 255))
        canvas.paste(photo, box[:2])
        Image.alpha_composite(canvas, frame).convert("RGB").save(out_path, quality=95)
        return {"prefitted": True}

    base = max(bw / photo.width, bh / photo.height)    # cover: เต็มกรอบ ไม่บิดสัดส่วน
    band_top, band_bot = overlay_band(frame, box)     # ช่วงที่ไม่โดนโลโก้/ป้าย (พิกัดในช่อง)
    m = skin_mask(photo)
    has_hand = m.mean() >= 0.03 and not focus
    if has_hand:
        ys, xs = np.nonzero(m)
        solid = np.nonzero(m.sum(axis=1) >= 0.03 * m.shape[1])[0]
        y_lo, y_hi = (solid.min(), solid.max()) if len(solid) else (ys.min(), ys.max())
        fx, fy_mid, fy_bot = xs.mean() / m.shape[1], ((y_lo + y_hi) / 2) / m.shape[0], y_hi / m.shape[0]
    elif focus:
        fx, fy_mid, fy_bot = focus[0], focus[1], None
    else:
        fx, fy_mid, fy_bot = 0.5, 0.5, None

    def layout(z):
        sw, sh = round(photo.width * base * z), round(photo.height * base * z)
        sx, sy = sw - bw, sh - bh
        ox = (fx * sw - bw / 2) if (has_hand or focus) else sx / 2
        oy = fy_mid * sh - (band_top + band_bot) / 2          # กลางมืออยู่กลางช่วงว่าง
        if fy_bot is not None:                                  # ปลายนิ้วล่างสุดต้องไม่ชนป้าย
            oy = max(oy, fy_bot * sh - band_bot)
        ox, oy = int(min(max(ox, 0), sx)), int(min(max(oy, 0), sy))
        clear = None if fy_bot is None else bool(fy_bot * sh - oy <= band_bot)
        return sw, sh, ox, oy, clear

    z = 1.0
    sw, sh, off_x, off_y, clear = layout(z)
    while clear is False and z < ZOOM_MAX - 1e-9:          # ไม่พอ -> ซูมเข้าทีละนิด
        z = round(z + 0.02, 2)
        sw, sh, off_x, off_y, clear = layout(z)

    fitted = photo.resize((sw, sh), Image.LANCZOS).crop((off_x, off_y, off_x + bw, off_y + bh))
    canvas = Image.new("RGBA", frame.size, (255, 255, 255, 255))
    canvas.paste(fitted, box[:2])
    Image.alpha_composite(canvas, frame).convert("RGB").save(out_path, quality=95)

    return {"band": [int(band_top), int(band_bot)], "zoom": z, "offset": [off_x, off_y], "hand_clear_of_overlays": clear}
