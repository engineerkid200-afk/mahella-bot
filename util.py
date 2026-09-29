import re
from html import escape

_MAP = {}
for _i, _ch in enumerate("۰۱۲۳۴۵۶۷۸۹"):
    _MAP[ord(_ch)] = str(_i)
for _i, _ch in enumerate("٠١٢٣٤٥٦٧٨٩"):
    _MAP[ord(_ch)] = str(_i)
_MAP.update({
    ord("ي"): "ی", ord("ك"): "ک", ord("ى"): "ی", ord("ۀ"): "ه", ord("ة"): "ه",
    0x200C: " ", 0x200D: None, 0x200E: None, 0x200F: None, 0x202B: None, 0x202C: None,
    0x066C: None, 0x066B: ".", 0x0640: None, ord("،"): ",",
})

_NUMTXT = re.compile(r"^-?[\d.,\s]+(?:تومان|ریال|عدد|ت)?$")


def norm(t):
    """متن را برای جستجو یکدست می‌کند (ارقام فارسی، ی/ک عربی، حروف کوچک)."""
    return " ".join(str(t if t is not None else "").translate(_MAP).lower().split())


def ncode(t):
    """کد محصول: بدون فاصله، حروف بزرگ، ارقام لاتین."""
    return "".join(str(t if t is not None else "").translate(_MAP).split()).upper()


def esc(v):
    return escape(str(v if v is not None else ""), quote=False)


def money(n):
    try:
        return f"{int(n):,}"
    except (TypeError, ValueError):
        return str(n)


def is_number(v):
    if v is None or isinstance(v, bool):
        return False
    if isinstance(v, (int, float)):
        return v == v
    s = str(v).translate(_MAP).strip()
    return bool(s) and bool(_NUMTXT.match(s)) and any(ch.isdigit() for ch in s)


def to_int(v, default=None):
    """هر چیزی شبیه عدد را به int تبدیل می‌کند: ۱٬۲۰۰٬۰۰۰ تومان، 12.5، '۳ عدد' ..."""
    if v is None or isinstance(v, bool):
        return default
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return default if v != v else int(round(v))
    s = str(v).translate(_MAP).replace(",", "").replace(" ", "")
    s = re.sub(r"[^\d.\-]", "", s)
    if not s or s in ("-", "."):
        return default
    if s.count(".") > 1:
        s = s.replace(".", "")
    elif "." in s:
        a, b = s.split(".")
        digits = a.lstrip("-")
        if len(b) == 3 and digits.isdigit() and 1 <= len(digits) <= 3:
            s = a + b
    try:
        return int(round(float(s)))
    except ValueError:
        return default
