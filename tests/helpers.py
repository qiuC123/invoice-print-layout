from __future__ import annotations

from pathlib import Path

import pymupdf


def make_trip_pdf(path: Path, pages: int = 1, amount: str = "123.45") -> Path:
    document = pymupdf.open()
    for index in range(pages):
        page = document.new_page(width=595.2756, height=841.8898)
        page.insert_text((72, 55), "DIDI BRAND ADVERTISEMENT", fontsize=14)
        page.insert_text((72, 85), "PROMOTION QR AREA", fontsize=10)
        if index == 0:
            page.insert_text((72, 180), "DIDI TRAVEL - TRIP TABLE", fontsize=18)
            page.insert_text((72, 215), f"TRIP TOTAL: {amount}", fontsize=12)
        else:
            page.insert_text((72, 180), f"TRIP PAGE {index + 1}", fontsize=18)
            page.insert_text((72, 215), "TRIP DETAILS", fontsize=12)
        page.insert_text((72, 260), "INDEX  PICKUP  DESTINATION  AMOUNT", fontsize=10)
        page.insert_text((72, 300), f"{index + 1}  START  END  {amount}", fontsize=10)
        page.draw_rect(pymupdf.Rect(65, 240, 530, 325), width=0.5)
        page.insert_text((470, 810), f"PAGE: {index + 1}/{pages}", fontsize=9)
    document.save(path)
    document.close()
    return path


def make_invoice_pdf(
    path: Path,
    amount: str = "123.45",
    invoice_number: str = "12345678901234567890",
    provider: str = "didi",
) -> Path:
    document = pymupdf.open()
    page = document.new_page(width=595.2756, height=409)
    page.insert_text((72, 60), "ELECTRONIC INVOICE", fontsize=18)
    if provider == "caocao":
        page.insert_text((350, 60), "GEELY RIDE", fontsize=10)
    page.insert_text((72, 100), f"INVOICE NUMBER: {invoice_number}", fontsize=11)
    page.insert_text((72, 130), "INVOICE DATE: 2026-01-15", fontsize=11)
    page.insert_text((72, 190), f"TOTAL WITH TAX: {amount}", fontsize=13)
    page.draw_rect(pymupdf.Rect(60, 40, 535, 340), width=0.7)
    document.save(path)
    document.close()
    return path


def make_caocao_trip_pdf(path: Path, amount: str = "52.14") -> Path:
    document = pymupdf.open()
    page = document.new_page(width=595.2756, height=841.8898)
    page.insert_text((72, 85), "CAOCAO TRAVEL - TRIP TABLE", fontsize=18)
    page.insert_text((72, 125), f"TRIP TOTAL: {amount}", fontsize=12)
    page.insert_text((72, 175), "INDEX  PICKUP  DESTINATION  AMOUNT", fontsize=10)
    page.insert_text((72, 210), f"1  START  END  {amount}", fontsize=10)
    page.draw_rect(pymupdf.Rect(65, 155, 530, 235), width=0.5)
    page.insert_text((470, 810), "PAGE: 1/1", fontsize=9)
    document.save(path)
    document.close()
    return path


def make_xiangdao_trip_pdf(path: Path, amount: str = "17.72") -> Path:
    document = pymupdf.open()
    page = document.new_page(width=595.2756, height=841.8898)
    page.insert_text((60, 100), "XIANGDAO TRAVEL - TRIP TABLE", fontsize=13)
    page.insert_text((60, 135), f"TRIP TOTAL: {amount}", fontsize=11)
    page.insert_text((70, 180), "INDEX  PICKUP  START  END  AMOUNT", fontsize=10)
    page.insert_text((75, 215), f"1  08-19 07:38  START  END  {amount}", fontsize=10)
    page.draw_rect(pymupdf.Rect(65, 160, 530, 240), width=0.5)
    page.insert_text((470, 810), "PAGE: 1/1", fontsize=9)
    document.save(path)
    document.close()
    return path


def make_xiangdao_invoice_pdf(path: Path, amount: str = "17.72") -> Path:
    document = pymupdf.open()
    page = document.new_page(width=595.28, height=510.24)
    page.insert_text((190, 40), "ELECTRONIC INVOICE", fontsize=18)
    page.insert_text((430, 60), "INVOICE NUMBER:", fontsize=9)
    page.insert_text((430, 80), "INVOICE DATE:", fontsize=9)
    page.insert_text((330, 115), "SELLER: XIANGDAO RIDE", fontsize=9)
    page.insert_text((48, 400), "TOTAL IN WORDS", fontsize=9)
    page.insert_text((405, 400), "(SMALL)", fontsize=9)
    page.insert_text((482, 60), "26000000000000000003", fontsize=9)
    page.insert_text((482, 80), "2026-09-04", fontsize=9)
    page.insert_text((440, 400), "CNY", fontsize=11)
    page.insert_text((450, 400), amount, fontsize=11)
    page.insert_text((60, 180), f"RIDE SERVICE  {amount}", fontsize=9)
    page.draw_rect(pymupdf.Rect(20, 90, 575, 450), width=0.5)
    document.save(path)
    document.close()
    return path


def make_generic_trip_pdf(path: Path, amount: str = "44.60") -> Path:
    document = pymupdf.open()
    page = document.new_page(width=595.2756, height=841.8898)
    page.insert_text((60, 100), "MYSTERY RIDE - TRIP TABLE", fontsize=15)
    page.insert_text((60, 135), f"TRIP TOTAL: {amount}", fontsize=11)
    page.insert_text(
        (70, 180),
        "INDEX  PICKUP  DESTINATION  AMOUNT",
        fontsize=10,
    )
    page.insert_text((75, 215), f"1  STATION  OFFICE  {amount}", fontsize=10)
    page.draw_rect(pymupdf.Rect(65, 160, 530, 240), width=0.5)
    page.insert_text((470, 810), "PAGE: 1/1", fontsize=9)
    document.save(path)
    document.close()
    return path
