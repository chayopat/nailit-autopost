from PIL import Image, ImageOps
import numpy as np

NAIL_MARGIN = 0.0   # เผื่อปลายเล็บที่อยู่ต่ำกว่าขอบมือ (สัดส่วนความสูงช่องรูป)
LOGO_PAD = 0.0     # เว้นระยะเหนือโลโก้

def skin_mask(img):
    s = img.convert("YCbCr"); s.thumbnail((300, 300))
    a = np.array(s).astype(int)
    Y, Cb, Cr = a[..., 0], a[..., 1], a[..., 2]
    return (Cr > 135) & (Cr < 180) & (Cb > 85) & (Cb < 135) & (Y > 60)

def logo_top(frame, box):
    """แถวบนสุดที่โลโก้ล้ำเข้ามาในช่องรูป (ไม่นับขอบกรอบ)"""
    a = np.array(frame.getchannel("A"))
    x0, y0, x1, y1 = box
    # ดูเฉพาะช่วงกลางกรอบ เพื่อไม่นับมุมโค้ง/ขอบ
    sub = a[y0 + (y1 - y0) // 2: y1 - 2, x0 + 150: x1 - 150] > 128
    rows = np.nonzero(sub.any(axis=1))[0]
    return None if len(rows) == 0 else y0 + (y1 - y0) // 2 + rows.min()

def compose(photo_path, frame_path, out_path, focus=None):
    frame = Image.open(frame_path).convert("RGBA")
    photo = ImageOps.exif_transpose(Image.open(photo_path)).convert("RGB")
    alpha = frame.getchannel("A")
    box = alpha.point(lambda v: 255 if v < 128 else 0).getbbox()
    bw, bh = box[2] - box[0], box[3] - box[1]

    # ขนาดรูปหลังขยายให้เต็มช่อง (cover)
    scale = max(bw / photo.width, bh / photo.height)
    sw, sh = round(photo.width * scale), round(photo.height * scale)
    slack_x, slack_y = sw - bw, sh - bh

    m = skin_mask(photo)
    ys, xs = np.nonzero(m) if m.mean() >= 0.03 else (None, None)
    if focus:
        fx, fy = focus
    elif ys is not None:
        fx, fy = xs.mean() / m.shape[1], ys.mean() / m.shape[0]
    else:
        fx, fy = 0.5, 0.5
    off_x = round(slack_x * min(max(fx, 0), 1)) if slack_x else 0
    off_y = round(slack_y * min(max(fy, 0), 1)) if slack_y else 0
    off_x = round(slack_x * 0.5) if slack_x and not focus else off_x

    # ดันรูปขึ้นให้ปลายนิ้วล่างสุดไม่ชนโลโก้ (เฉพาะรูปที่ครอปแนวตั้งได้)
    lt = logo_top(frame, box)
    if lt is not None and ys is not None and slack_y > 0 and not focus:
        rows = m.sum(axis=1)
        solid = np.nonzero(rows >= 0.03 * m.shape[1])[0]            # ตัดจุดรบกวนเล็กๆ ออก
        low = solid.max() if len(solid) else ys.max()
        bottom = (low / m.shape[0]) * sh + NAIL_MARGIN * bh         # ปลายเล็บล่างสุด (พิกัดรูปที่ขยายแล้ว)
        need = bottom - (lt - box[1] - LOGO_PAD * bh)              # ต้องเลื่อนขึ้นเท่าไร
        # ไม่เลื่อนจนมือบนถูกตัดเกินไป: ขอบบนของผิวหนังต้องไม่ถูกตัดเกิน 0
        off_y = int(min(max(off_y, need), slack_y))

    fitted = photo.resize((sw, sh), Image.LANCZOS).crop((off_x, off_y, off_x + bw, off_y + bh))
    canvas = Image.new("RGBA", frame.size, (255, 255, 255, 255))
    canvas.paste(fitted, box[:2])
    Image.alpha_composite(canvas, frame).convert("RGB").save(out_path, quality=95)
    return {"offset_y_pct": round(off_y / slack_y, 2) if slack_y else 0, "logo_top": lt}
