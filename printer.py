"""
printer.py — Impressão na Citizen (CY-02 e similares) via Win32 GDI.

Responsabilidades:
- Selecionar a impressora (autodetect ou nome explícito).
- Pré-checar status / DPI / área imprimível.
- Imprimir uma imagem (bytes) em papel 4x6, detectando orientação.

Não sobrescreve DEVMODE — respeita configurações do driver (glossy/matte,
ribbon rewind 6x8 → 2x 4x6, etc.). Cada chamada a print_image() gera
um job separado, condição necessária pro ribbon rewind funcionar.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from io import BytesIO
from typing import List, Optional

from PIL import Image, ImageOps, ImageWin

try:
    import win32con
    import win32print
    import win32ui
except ImportError as e:  # pragma: no cover
    raise ImportError(
        "pywin32 não está instalado ou não está disponível neste SO. "
        "Este agente roda apenas em Windows. Rode: pip install pywin32"
    ) from e


log = logging.getLogger(__name__)

# Constantes para Citizen 4x6 @ 300 DPI
TARGET_DPI = 300
TARGET_W_PORTRAIT = 1248   # 4" * 300 + bleed
TARGET_H_PORTRAIT = 1844   # 6" * 300 + bleed
CITIZEN_PREFIX = "CITIZEN"


class PrinterError(Exception):
    """Erro de impressão (driver, papel, ribbon, offline, etc.)."""


@dataclass
class PrinterCaps:
    name: str
    dpi_x: int
    dpi_y: int
    horz_res: int       # pixels imprimíveis
    vert_res: int
    phys_width: int     # pixels físicos do papel
    phys_height: int
    status_raw: int
    status_text: str

    @property
    def is_offline_or_error(self) -> bool:
        bad_bits = (
            0x00000080  # OFFLINE
            | 0x00000002  # ERROR
            | 0x00000008  # PAPER_JAM
            | 0x00000010  # PAPER_OUT
            | 0x00040000  # NO_TONER (ribbon)
            | 0x00000400  # OUT_OF_MEMORY
            | 0x00400000  # DOOR_OPEN
        )
        return bool(self.status_raw & bad_bits)


# --------------------------------------------------------------------------- #
# Seleção / listagem de impressoras
# --------------------------------------------------------------------------- #

def list_installed_printers() -> List[str]:
    """Lista todas as impressoras locais + de rede visíveis pelo usuário atual."""
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    return [p[2] for p in win32print.EnumPrinters(flags)]


def autodetect_citizen(printers: Optional[List[str]] = None) -> List[str]:
    printers = printers if printers is not None else list_installed_printers()
    return [p for p in printers if p.upper().startswith(CITIZEN_PREFIX)]


def resolve_printer_name(configured: str) -> str:
    """
    Aplica a regra de seleção:
    - Se configured: tem que existir nas instaladas.
    - Senão: autodetect Citizen; exatamente 1 → ok; 0 ou 2+ → erro.
    """
    installed = list_installed_printers()
    if not installed:
        raise PrinterError("Nenhuma impressora instalada no Windows.")

    if configured:
        if configured in installed:
            return configured
        raise PrinterError(
            f"Impressora '{configured}' não encontrada. "
            f"Disponíveis: {installed}"
        )

    citizens = autodetect_citizen(installed)
    if len(citizens) == 1:
        return citizens[0]
    if not citizens:
        raise PrinterError(
            "Nenhuma impressora Citizen detectada (autodetect). "
            f"Disponíveis: {installed}. "
            "Preencha PRINTER_NAME no .env."
        )
    raise PrinterError(
        f"Múltiplas impressoras Citizen detectadas: {citizens}. "
        "Preencha PRINTER_NAME no .env com o nome exato."
    )


# --------------------------------------------------------------------------- #
# Pré-checagem (status + caps)
# --------------------------------------------------------------------------- #

_STATUS_BITS = {
    0x00000001: "PAUSED",
    0x00000002: "ERROR",
    0x00000004: "PENDING_DELETION",
    0x00000008: "PAPER_JAM",
    0x00000010: "PAPER_OUT",
    0x00000020: "MANUAL_FEED",
    0x00000040: "PAPER_PROBLEM",
    0x00000080: "OFFLINE",
    0x00000100: "IO_ACTIVE",
    0x00000200: "BUSY",
    0x00000400: "PRINTING",
    0x00000800: "OUTPUT_BIN_FULL",
    0x00001000: "NOT_AVAILABLE",
    0x00002000: "WAITING",
    0x00004000: "PROCESSING",
    0x00008000: "INITIALIZING",
    0x00010000: "WARMING_UP",
    0x00020000: "TONER_LOW",
    0x00040000: "NO_TONER",
    0x00080000: "PAGE_PUNT",
    0x00100000: "USER_INTERVENTION",
    0x00200000: "OUT_OF_MEMORY",
    0x00400000: "DOOR_OPEN",
}


def _status_to_text(status: int) -> str:
    if status == 0:
        return "READY"
    return "|".join(name for bit, name in _STATUS_BITS.items() if status & bit) or f"0x{status:08X}"


def check_printer(name: str) -> PrinterCaps:
    """
    Lê status + DPI + área imprimível. Não imprime nada.
    Levanta PrinterError se não conseguir abrir o handle/DC.
    """
    try:
        h = win32print.OpenPrinter(name)
    except Exception as e:
        raise PrinterError(f"Falha ao abrir impressora '{name}': {e}") from e
    try:
        info = win32print.GetPrinter(h, 2)
        status_raw = int(info.get("Status", 0))
    finally:
        win32print.ClosePrinter(h)

    dc = win32ui.CreateDC()
    try:
        dc.CreatePrinterDC(name)
        dpi_x = dc.GetDeviceCaps(win32con.LOGPIXELSX)
        dpi_y = dc.GetDeviceCaps(win32con.LOGPIXELSY)
        horz_res = dc.GetDeviceCaps(win32con.HORZRES)
        vert_res = dc.GetDeviceCaps(win32con.VERTRES)
        phys_w = dc.GetDeviceCaps(win32con.PHYSICALWIDTH)
        phys_h = dc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
    except Exception as e:
        raise PrinterError(f"Falha ao criar DC para '{name}': {e}") from e
    finally:
        try:
            dc.DeleteDC()
        except Exception:
            pass

    return PrinterCaps(
        name=name,
        dpi_x=dpi_x,
        dpi_y=dpi_y,
        horz_res=horz_res,
        vert_res=vert_res,
        phys_width=phys_w,
        phys_height=phys_h,
        status_raw=status_raw,
        status_text=_status_to_text(status_raw),
    )


# --------------------------------------------------------------------------- #
# Pipeline de imagem
# --------------------------------------------------------------------------- #

def _prepare_image(img_bytes: bytes) -> Image.Image:
    """EXIF transpose + RGB + crop centralizado 4:6 + resize 300 DPI."""
    img = Image.open(BytesIO(img_bytes))
    img = ImageOps.exif_transpose(img)
    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    landscape = w > h

    if landscape:
        target_w, target_h = TARGET_H_PORTRAIT, TARGET_W_PORTRAIT  # 1844 x 1248
    else:
        target_w, target_h = TARGET_W_PORTRAIT, TARGET_H_PORTRAIT  # 1248 x 1844

    target_ratio = target_w / target_h
    src_ratio = w / h
    if abs(src_ratio - target_ratio) > 1e-3:
        if src_ratio > target_ratio:
            new_w = int(round(h * target_ratio))
            x0 = (w - new_w) // 2
            img = img.crop((x0, 0, x0 + new_w, h))
        else:
            new_h = int(round(w / target_ratio))
            y0 = (h - new_h) // 2
            img = img.crop((0, y0, w, y0 + new_h))

    img = img.resize((target_w, target_h), Image.LANCZOS)
    return img


# --------------------------------------------------------------------------- #
# Impressão
# --------------------------------------------------------------------------- #

def print_image(img_bytes: bytes, printer_name: str, *, doc_name: str = "PhotoFlow") -> None:
    """
    Imprime imagem na impressora Citizen em papel 4x6.
    Detecta orientação automaticamente (retrato vs paisagem).
    Levanta PrinterError em qualquer falha.

    Usa ImageWin.Dib (Pillow) para renderizar no DC — forma correta
    com pywin32, que não expõe StretchDIBits diretamente no PyCDC.
    """
    img = _prepare_image(img_bytes)
    img_w, img_h = img.size
    landscape = img_w > img_h

    log.info(
        "print_prepare",
        extra={
            "event": "print_prepare",
            "orientation": "landscape" if landscape else "portrait",
            "img_w": img_w,
            "img_h": img_h,
        },
    )

    dc = win32ui.CreateDC()
    try:
        try:
            dc.CreatePrinterDC(printer_name)
        except Exception as e:
            raise PrinterError(f"Não foi possível abrir DC da impressora: {e}") from e

        dpi_x = dc.GetDeviceCaps(win32con.LOGPIXELSX)
        dpi_y = dc.GetDeviceCaps(win32con.LOGPIXELSY)
        horz_res = dc.GetDeviceCaps(win32con.HORZRES)
        vert_res = dc.GetDeviceCaps(win32con.VERTRES)
        phys_w = dc.GetDeviceCaps(win32con.PHYSICALWIDTH)
        phys_h = dc.GetDeviceCaps(win32con.PHYSICALHEIGHT)
        offset_x = dc.GetDeviceCaps(win32con.PHYSICALOFFSETX)
        offset_y = dc.GetDeviceCaps(win32con.PHYSICALOFFSETY)

        if dpi_x != TARGET_DPI or dpi_y != TARGET_DPI:
            log.warning(
                "print_dpi_mismatch",
                extra={
                    "event": "print_dpi_mismatch",
                    "dpi_x": dpi_x,
                    "dpi_y": dpi_y,
                    "expected": TARGET_DPI,
                },
            )

        # Calcula área de destino centralizada dentro da área imprimível
        src_ratio = img_w / img_h
        if horz_res / vert_res > src_ratio:
            draw_h = vert_res
            draw_w = int(round(draw_h * src_ratio))
        else:
            draw_w = horz_res
            draw_h = int(round(draw_w / src_ratio))

        x = (horz_res - draw_w) // 2
        y = (vert_res - draw_h) // 2

        log.info(
            "print_start",
            extra={
                "event": "print_start",
                "printer": printer_name,
                "dpi_x": dpi_x,
                "dpi_y": dpi_y,
                "horz_res": horz_res,
                "vert_res": vert_res,
                "phys_w": phys_w,
                "phys_h": phys_h,
                "offset_x": offset_x,
                "offset_y": offset_y,
                "draw_w": draw_w,
                "draw_h": draw_h,
                "x": x,
                "y": y,
            },
        )

        # ImageWin.Dib é a forma canônica de renderizar Pillow num DC Win32.
        # Recebe o handle do DC (inteiro) e o retângulo de destino.
        dib = ImageWin.Dib(img)
        handle = dc.GetHandleOutput()  # HDC como inteiro

        try:
            dc.StartDoc(doc_name)
        except Exception as e:
            raise PrinterError(f"StartDoc falhou: {e}") from e

        page_started = False
        try:
            dc.StartPage()
            page_started = True

            dib.draw(handle, (x, y, x + draw_w, y + draw_h))

            dc.EndPage()
            page_started = False
            dc.EndDoc()
        except Exception as e:
            try:
                if page_started:
                    dc.EndPage()
            except Exception:
                pass
            try:
                dc.AbortDoc()
            except Exception:
                pass
            raise PrinterError(f"Falha durante impressão: {e}") from e

        log.info("print_done", extra={"event": "print_done", "printer": printer_name})

    finally:
        try:
            dc.DeleteDC()
        except Exception:
            pass