#!/usr/bin/env python3
"""
QROS Generator - Generátor kazetových obrazov pre QROS/EMO
============================================================
Konvertuje binárne spustiteľné súbory Atari 8-bit (XEX/COM/OBX)
na kazetové obrazy CAS a WAV pre turbo systém QROS/EMO.

Verzia 0.5 — refaktorovaná, s overovaním výstupu a CLI režimom.
Formát výstupu je bit-for-bit zhodný s v0.4 (overené proti Decathlon.cas).

QROS systém & SW: Ing. Matúš Žúbor
QROS HW oživenie (2025): Aleister
QROS Generator: W1K
Referenčný kód: Turgen System (baktra)

Použitie:
    python qros_generator-v0.5.py                 → grafické rozhranie
    python qros_generator-v0.5.py hra.xex --wav   → príkazový riadok
"""

import os
import sys
import array
import struct
import wave
import argparse
import threading
import unicodedata

try:
    import tkinter as tk
    from tkinter import ttk, filedialog, messagebox
    HAVE_TK = True
except ImportError:                     # headless systém — funguje aspoň CLI
    HAVE_TK = False

APP_NAME = "QROS Generator"
APP_VERSION = "0.5"


# =============================================================================
# Konštanty QROS/EMO
# =============================================================================

# POKEY generuje baud rate cez delič: baud = clock / (2 × (AUDF + 7))
# Deliče z originálnej dokumentácie (Ing. Žúbor, XIO 22 príkaz):
#   6600 režim: AUDF = 127 (rychlL=127, rychlH=0)
#   9600 režim: AUDF = 86  (rychlL=86,  rychlH=0)
#  32000 režim: AUDF = 5   (rychlL=5,   rychlH=0)
POKEY_CLOCK_PAL = 1773447       # PAL POKEY clock (Hz)
POKEY_CLOCK_NTSC = 1789773      # NTSC POKEY clock (Hz)
POKEY_AUDF_6600 = 127           # delič pre "6600 baud" režim
POKEY_AUDF_9600 = 86            # delič pre "9600 baud" režim
POKEY_AUDF_32000 = 5            # delič pre "32000 baud" režim


def pokey_baud(audf, clock=POKEY_CLOCK_PAL):
    """Vypočíta presnú POKEY prenosovú rýchlosť z AUDF deliča."""
    return clock / (2 * (audf + 7))


# Presné POKEY rýchlosti (PAL)
POKEY_EXACT_6600_PAL = round(pokey_baud(POKEY_AUDF_6600, POKEY_CLOCK_PAL))   # 6617
POKEY_EXACT_9600_PAL = round(pokey_baud(POKEY_AUDF_9600, POKEY_CLOCK_PAL))   # 9535
# Presné POKEY rýchlosti (NTSC)
POKEY_EXACT_6600_NTSC = round(pokey_baud(POKEY_AUDF_6600, POKEY_CLOCK_NTSC)) # 6678
POKEY_EXACT_9600_NTSC = round(pokey_baud(POKEY_AUDF_9600, POKEY_CLOCK_NTSC)) # 9622

# Empiricky overené hodnoty pre CAS baud chunk
QROS_BAUD_6595 = 6595           # empiricky funguje s PAL Atari
QROS_BAUD_9600 = 9600           # štandardná hodnota
QROS_DEFAULT_BAUD = QROS_BAUD_6595

# Zoznam ponúkaných rýchlostí: (hodnota, popis)
QROS_BAUD_CHOICES = [
    (QROS_BAUD_6595, "empirická, overená s PAL Atari"),
    (POKEY_EXACT_6600_PAL, "presný POKEY delič 127 (PAL)"),
    (POKEY_EXACT_6600_NTSC, "presný POKEY delič 127 (NTSC)"),
    (QROS_BAUD_9600, "štandardná hodnota"),
    (POKEY_EXACT_9600_PAL, "presný POKEY delič 86 (PAL)"),
    (POKEY_EXACT_9600_NTSC, "presný POKEY delič 86 (NTSC)"),
]

QROS_BLOCK_SIZE = 128           # počet dátových bajtov v bloku
QROS_RECORD_SIZE = 132          # 2 index + 1 typ + 128 dát + 1 checksum
QROS_FRAME_BITS = 10            # SIO rámec: štart + 8 dát + stop

# Typy blokov
QROS_BLOCK_COMPLETE = 0xFC      # kompletný dátový blok (128 bajtov)
QROS_BLOCK_PARTIAL = 0xFA       # nekompletný blok (menej ako 128 bajtov)
QROS_BLOCK_EOF = 0xFE           # koniec súboru
QROS_BLOCK_HEADER = 0xFD        # hlavičkový blok s boot loaderom
QROS_BLOCK_BLANK = 0xFF         # prázdny blok (nultý, nastavovací)

QROS_BLOCK_NAMES = {
    QROS_BLOCK_COMPLETE: "dátový úplný",
    QROS_BLOCK_PARTIAL: "dátový neúplný",
    QROS_BLOCK_EOF: "koncový",
    QROS_BLOCK_HEADER: "informačný (hlavička)",
    QROS_BLOCK_BLANK: "nastavovací (prázdny)",
}

# Medzery medzi záznamami (Inter-Record Gap) v milisekundách
QROS_IRG_HEADER = 1258          # pred prvým záznamom na páske
QROS_IRG_HEADER_REPEAT = 39     # pred opakovaním hlavičky
QROS_IRG_DATA = 39              # pred dátovými blokmi

# Index hlavičkového bloku (v šablóne pevne 0x0001), dáta začínajú od 2
QROS_HEADER_INDEX = 1
QROS_FIRST_DATA_INDEX = 2

# Umiestnenie názvu v hlavičkovom bloku
QROS_NAME_OFFSET = 0x08         # prvý bajt názvu
QROS_NAME_LENGTH = 25           # maximálna dĺžka názvu na páske
QROS_NAME_EOL = 0x9B            # ATASCII EOL za názvom (offset 0x21)

# Šablóna hlavičky QROS (132 bajtov) — obsahuje kód boot loadera
QROS_HEADER_TEMPLATE = bytes([
    0x00, 0x01, 0xFD, 0x42, 0x2A, 0x20, 0x20, 0x20, 0x42, 0x4F, 0x55, 0x4C, 0x44, 0x45, 0x52, 0x44,
    0x41, 0x53, 0x48, 0x9B, 0x00, 0x9B, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00,
    0x00, 0x00, 0x00, 0x10, 0x12, 0x0E, 0x10, 0x12, 0x0E, 0x12, 0x15, 0x00, 0xFD, 0x7B, 0x9A, 0x00,
    0x0D, 0x01, 0x40, 0x01, 0x85, 0x03, 0x0A, 0x01, 0x61, 0x04, 0x89, 0x40, 0x6D, 0xE0, 0xE3, 0x6D,
    0xCA, 0xE3, 0x80, 0xE2, 0x91, 0x04, 0x79, 0x9E, 0xE3, 0xA8, 0xA0, 0xFF, 0x90, 0xE4, 0xA9, 0x7B,
    0xB0, 0xD2, 0x89, 0x7B, 0x79, 0x9F, 0xE3, 0x00, 0xEB, 0x03, 0x86, 0x0E, 0x9D, 0x2A, 0xE3, 0x28,
    0x09, 0xE4, 0xB0, 0xE7, 0x00, 0x03, 0x03, 0x89, 0x60, 0xB0, 0xF9, 0x82, 0xE2, 0x9D, 0x83, 0x03,
    0x7D, 0x80, 0x03, 0xAA, 0xF0, 0xD7, 0xC8, 0x6A, 0x65, 0x21, 0x7D, 0xD8, 0x0C, 0xC8, 0xC0, 0x08,
    0xB0, 0xD8, 0x89, 0xC4
])

# Bootovacia páska: štandardné 600-baud záznamy s naším loaderom pred QROS dátami
CAS_BOOT_BAUD = 600             # rýchlosť, ktorou OS číta bootovací záznam
CAS_BOOT_SYNC = 0x55            # dva synchronizačné bajty štandardného záznamu
BOOT_IRG_FIRST = 9600           # dlhý nábeh pred prvým záznamom (ms)
BOOT_IRG_NEXT = 250             # medzera medzi bootovacími záznamami (ms)
BOOT_LOADER_FILE = "qrosload.bin"

# Parametre WAV výstupu
WAV_SAMPLE_RATE = 96000         # predvolená vzorkovacia frekvencia (Hz)
WAV_AMPLITUDE = 20000           # predvolená amplitúda (16-bit signed, max 32767)
WAV_TAIL_MS = 500               # ticho na konci nahrávky
WAV_RATE_CHOICES = [44100, 48000, 96000, 192000]


# =============================================================================
# ATASCII — prevod názvu na pásku
# =============================================================================

# Znaky, ktoré Unicode dekompozícia nerozloží (nemajú kombinovaný diakritický znak)
_ATASCII_MAP = {
    'ď': 'd', 'Ď': 'D', 'ť': 't', 'Ť': 'T', 'ľ': 'l', 'Ľ': 'L',
    'đ': 'd', 'Đ': 'D', 'ł': 'l', 'Ł': 'L', 'ß': 'ss',
    'æ': 'ae', 'Æ': 'AE', 'ø': 'o', 'Ø': 'O', 'œ': 'oe', 'Œ': 'OE',
}


def to_atascii(text, length=QROS_NAME_LENGTH, pad=0x20):
    """
    Prevedie textový názov na ATASCII bajty pre hlavičku pásky.

    Slovenská diakritika sa prepíše na základné písmená (Ž → Z, č → c),
    neznáme znaky na '?'. Výsledok je doplnený medzerami na `length`.
    Vracia (bajty, bol_skratený).
    """
    out = []
    for ch in text:
        repl = _ATASCII_MAP.get(ch)
        if repl is None:
            # Rozlož na základný znak + diakritiku a diakritiku zahoď
            decomposed = unicodedata.normalize('NFD', ch)
            repl = ''.join(c for c in decomposed if not unicodedata.combining(c))
        for c in repl:
            code = ord(c)
            # ATASCII 0x20–0x7E sa zhoduje s ASCII; ostatné nahradíme
            out.append(code if 0x20 <= code <= 0x7E else 0x3F)

    truncated = len(out) > length
    out = out[:length]
    out += [pad] * (length - len(out))
    return bytes(out), truncated


# =============================================================================
# Parser binárnych súborov XEX/COM
# =============================================================================

class Segment:
    """Jeden segment Atari binárneho súboru (adresa začiatku, konca a dáta)."""

    def __init__(self, start_addr, end_addr, data):
        self.start_addr = start_addr
        self.end_addr = end_addr
        self.data = data

    @property
    def length(self):
        return len(self.data)

    def __repr__(self):
        return f"${self.start_addr:04X}-${self.end_addr:04X} ({self.length} bytes)"


class XEXFile:
    """
    Parsuje Atari binárny súbor (XEX/COM/OBX).
    Extrahuje segmenty, RUN adresu ($02E0) a INIT adresy ($02E2).

    Pozor: QROS boot loader nahráva SUROVÉ bajty súboru — parsovanie
    slúži iba na analýzu a kontrolu, nie na tvorbu blokov.
    Prípadné nezrovnalosti sa nezahadzujú ticho, ale zbierajú do
    zoznamu `warnings`.
    """

    def __init__(self, filename):
        self.filename = filename
        self.segments = []
        self.run_address = None
        self.init_addresses = []
        self.warnings = []
        with open(filename, 'rb') as f:
            self.raw = bytearray(f.read())
        self._parse()

    def _parse(self):
        """Rozloží načítané bajty na segmenty."""
        data = self.raw
        if len(data) < 6:
            raise ValueError("Neplatný XEX: súbor je príliš krátky")
        if not (data[0] == 0xFF and data[1] == 0xFF):
            raise ValueError("Neplatný XEX: chýba FF FF hlavička")

        pos = 0
        while pos + 1 < len(data):
            # FF FF hlavička je povinná na začiatku, ďalej voliteľná
            if data[pos] == 0xFF and data[pos + 1] == 0xFF:
                pos += 2
                continue

            if pos + 4 > len(data):
                self.warnings.append(
                    f"Neúplná hlavička segmentu na offsete {pos} "
                    f"({len(data) - pos} zvyšných bajtov)")
                break

            start_addr = data[pos] | (data[pos + 1] << 8)
            end_addr = data[pos + 2] | (data[pos + 3] << 8)
            pos += 4

            if end_addr < start_addr:
                raise ValueError(
                    f"Neplatný segment: ${end_addr:04X} < ${start_addr:04X}")

            seg_len = end_addr - start_addr + 1
            if pos + seg_len > len(data):
                missing = pos + seg_len - len(data)
                self.warnings.append(
                    f"Segment ${start_addr:04X}-${end_addr:04X} je skrátený "
                    f"o {missing} bajtov (súbor končí skôr)")
                seg_len = len(data) - pos

            seg_data = data[pos:pos + seg_len]
            pos += seg_len

            # Špeciálne adresy: RUN ($02E0) a INIT ($02E2)
            if start_addr == 0x02E0 and end_addr == 0x02E1 and len(seg_data) >= 2:
                self.run_address = seg_data[0] | (seg_data[1] << 8)
            elif start_addr == 0x02E2 and end_addr == 0x02E3 and len(seg_data) >= 2:
                self.init_addresses.append(seg_data[0] | (seg_data[1] << 8))
            else:
                self.segments.append(Segment(start_addr, end_addr, seg_data))

        if pos < len(data):
            self.warnings.append(
                f"Za posledným segmentom zostalo {len(data) - pos} nespracovaných bajtov")
        if not self.segments:
            self.warnings.append("Súbor neobsahuje žiadny dátový segment")
        if self.run_address is None and not self.init_addresses:
            self.warnings.append(
                "Chýba RUN ($02E0) aj INIT ($02E2) — program sa nemusí spustiť")

    @property
    def size(self):
        return len(self.raw)

    def get_raw_file_data(self):
        """Vráti surové bajty celého súboru (QROS nahráva celý XEX ako raw dáta)."""
        return self.raw

    def default_tape_name(self):
        """Odvodí názov na pásku z názvu súboru."""
        return os.path.splitext(os.path.basename(self.filename))[0]


# =============================================================================
# SIO Checksum (kontrolný súčet pre QROS/EMO)
# =============================================================================

def sio_checksum(data):
    """
    SIO kontrolný súčet: súčet s prenosom carry.
    Každý bajt sa pripočíta, pri pretečení cez 255 sa pridá 1.
    """
    total = 0
    for b in data:
        total += (b & 0xFF)
        if total > 255:
            total = (total & 0xFF) + 1
    return total & 0xFF


# =============================================================================
# Zápis CAS súborov pre QROS/EMO
# =============================================================================

class QROSCASWriter:
    """
    Vytvára CAS súbory s QROS/EMO chunkami.
    Formát CAS chunku: [4B ID][2B dĺžka LE][2B aux LE][dáta]
    """

    def __init__(self):
        self.chunks = bytearray()

    def add_fuji(self, description=""):
        """Pridá FUJI chunk — popis/identifikácia CAS súboru."""
        desc = description.encode('ascii', errors='replace')
        self.chunks += b'FUJI'
        self.chunks += struct.pack('<HH', len(desc), 0)
        self.chunks += desc

    def add_baud(self, baudrate=QROS_DEFAULT_BAUD):
        """Pridá baud chunk — nastaví prenosovú rýchlosť pre nasledujúce dáta."""
        self.chunks += b'baud'
        self.chunks += struct.pack('<HH', 0, baudrate)

    def add_data(self, data, irg_ms=0):
        """Pridá data chunk s IRG (medzera medzi záznamami) v milisekundách."""
        self.chunks += b'data'
        self.chunks += struct.pack('<HH', len(data), irg_ms)
        self.chunks += data

    def write(self, filename):
        """Zapíše CAS súbor na disk."""
        with open(filename, 'wb') as f:
            f.write(self.chunks)

    def __len__(self):
        return len(self.chunks)


def parse_cas(data):
    """
    Rozloží CAS súbor na chunky. Vracia zoznam (id, aux, dáta).
    Vyhodí ValueError pri poškodenej štruktúre.
    """
    chunks = []
    pos = 0
    while pos < len(data):
        if pos + 8 > len(data):
            raise ValueError(
                f"Useknutá hlavička chunku na offsete {pos} "
                f"({len(data) - pos} bajtov)")
        chunk_id = bytes(data[pos:pos + 4])
        length, aux = struct.unpack('<HH', data[pos + 4:pos + 8])
        pos += 8
        if pos + length > len(data):
            raise ValueError(
                f"Chunk {chunk_id.decode('latin1')} na offsete {pos - 8} "
                f"presahuje koniec súboru")
        chunks.append((chunk_id, aux, bytes(data[pos:pos + length])))
        pos += length
    return chunks


# =============================================================================
# Bootovací zavádzač (qrosload.asm preložený cez mads.exe)
# =============================================================================

def pokey_divisor(baudrate, clock=POKEY_CLOCK_PAL):
    """
    Vráti POKEY delič pre zadanú rýchlosť: baud = clock / (2 × (delič + 7)).
    Pre dokumentované režimy vracia hodnoty z originálnej dokumentácie.
    """
    if baudrate in (QROS_BAUD_6595, POKEY_EXACT_6600_PAL, POKEY_EXACT_6600_NTSC):
        return POKEY_AUDF_6600
    if baudrate in (QROS_BAUD_9600, POKEY_EXACT_9600_PAL, POKEY_EXACT_9600_NTSC):
        return POKEY_AUDF_9600
    divisor = int(round(clock / (2.0 * baudrate))) - 7
    return max(0, min(0xFFFF, divisor))


def load_boot_loader(path=None):
    """
    Načíta preložený loader (qrosload.bin) spolu s jeho boot hlavičkou.
    Hľadá ho vedľa skriptu, ak nie je zadaná cesta.
    """
    if path is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            BOOT_LOADER_FILE)
    if not os.path.isfile(path):
        raise FileNotFoundError(
            f"Chýba {os.path.basename(path)} — najprv spustite: python build_loader.py")
    with open(path, 'rb') as f:
        blob = bytearray(f.read())
    if len(blob) < 8 or blob[0] != 0x00:
        raise ValueError(f"{os.path.basename(path)} nevyzerá ako bootovací blob")
    return blob


def make_std_record(data, ctrl=QROS_BLOCK_COMPLETE):
    """
    Štandardný 132-bajtový kazetový záznam Atari:
    [0x55][0x55][riadiaci bajt][128 dát][SIO checksum].
    QROS blok má na mieste synchronizačných bajtov index — inak je zhodný.
    """
    if len(data) != QROS_BLOCK_SIZE:
        raise ValueError(f"Záznam musí mať {QROS_BLOCK_SIZE} bajtov, má {len(data)}")
    record = bytearray([CAS_BOOT_SYNC, CAS_BOOT_SYNC, ctrl])
    record += bytes(data)
    record.append(sio_checksum(record))
    return record


def build_boot_records(blob, total_blocks=0, divisor=POKEY_AUDF_6600):
    """
    Rozdelí loader na štandardné bootovacie záznamy a dopíše doň parametre
    pásky (delič rýchlosti a počet dátových blokov pre zobrazenie percent).
    """
    blob = bytearray(blob)
    blob[-4] = divisor & 0xFF           # divisor  (offset dĺžka-4)
    blob[-3] = divisor >> 8
    blob[-2] = total_blocks & 0xFF      # total_blk (offset dĺžka-2)
    blob[-1] = (total_blocks >> 8) & 0xFF

    count = blob[1]                     # počet záznamov si spočítal už mads
    padded = blob + bytes(count * QROS_BLOCK_SIZE - len(blob))
    return [make_std_record(padded[i * QROS_BLOCK_SIZE:(i + 1) * QROS_BLOCK_SIZE])
            for i in range(count)]


def rename_cas(cas_bytes, new_name):
    """
    Prepíše názov v hlavičkových blokoch hotového QROS obrazu.
    Pásky z iných generátorov mávajú pole názvu prázdne — loader potom
    nemá čo zobraziť, lebo FUJI chunk sa na pásku nenahráva.
    Vracia (nové bajty, počet zmenených hlavičiek).
    """
    chunks = parse_cas(cas_bytes)
    name_bytes, _truncated = to_atascii(new_name)
    out = QROSCASWriter()
    changed = 0

    for chunk_id, aux, body in chunks:
        if chunk_id == b'FUJI':
            out.add_fuji(new_name)
        elif chunk_id == b'baud':
            out.add_baud(aux)
        elif chunk_id == b'data':
            block = bytearray(body)
            if (len(block) == QROS_RECORD_SIZE
                    and block[2] == QROS_BLOCK_HEADER
                    and not is_boot_record(block)):
                block[QROS_NAME_OFFSET:QROS_NAME_OFFSET + QROS_NAME_LENGTH] = name_bytes
                block[QROS_NAME_OFFSET + QROS_NAME_LENGTH] = QROS_NAME_EOL
                block[-1] = sio_checksum(block[:-1])
                changed += 1
            out.add_data(bytes(block), aux)
        else:
            out.chunks += chunk_id + struct.pack('<HH', len(body), aux) + body

    return bytes(out.chunks), changed


def is_boot_record(block):
    """Rozpozná štandardný bootovací záznam podľa synchronizačných bajtov."""
    return (len(block) == QROS_RECORD_SIZE
            and block[0] == CAS_BOOT_SYNC and block[1] == CAS_BOOT_SYNC)


# =============================================================================
# Tvorba QROS blokov
# =============================================================================

def make_qros_blank_block():
    """Vytvorí nastavovací blok (index 0, typ $FF, všetky dátové bajty $FF)."""
    block = bytearray([0x00, 0x00, QROS_BLOCK_BLANK])
    block += b'\xFF' * QROS_BLOCK_SIZE
    block.append(sio_checksum(block))
    return block


def make_qros_header(name):
    """
    Vytvorí hlavičkový blok QROS (132 bajtov).
    Vychádza zo šablóny s boot loader kódom.
    Názov sa umiestni na offset 0x08 (25 znakov ATASCII + 0x9B terminátor).
    """
    header = bytearray(QROS_HEADER_TEMPLATE)

    name_bytes, _ = to_atascii(name, QROS_NAME_LENGTH)
    header[QROS_NAME_OFFSET:QROS_NAME_OFFSET + QROS_NAME_LENGTH] = name_bytes
    header[QROS_NAME_OFFSET + QROS_NAME_LENGTH] = QROS_NAME_EOL

    # Prepočítanie SIO checksumu (posledný bajt)
    header[-1] = sio_checksum(header[:-1])
    return header


def make_qros_data_block(block_index, data, block_type=QROS_BLOCK_COMPLETE):
    """
    Vytvorí 132-bajtový QROS dátový blok.
    [0]     = index bloku (vysoký bajt)
    [1]     = index bloku (nízky bajt)
    [2]     = typ bloku (FC=kompletný, FA=čiastočný, FE=koniec)
    [3-130] = 128 dátových bajtov
    [131]   = SIO kontrolný súčet
    """
    if not 0 <= block_index <= 0xFFFF:
        raise ValueError(f"Index bloku mimo rozsahu: {block_index}")

    block = bytearray([block_index >> 8, block_index & 0xFF, block_type])

    if block_type == QROS_BLOCK_COMPLETE:
        if len(data) != QROS_BLOCK_SIZE:
            raise ValueError(
                f"Kompletný blok musí mať {QROS_BLOCK_SIZE} bajtov, má {len(data)}")
        block += bytes(data)
    elif block_type == QROS_BLOCK_PARTIAL:
        # Čiastočný blok — posledný bajt nesie počet platných dát
        valid_bytes = len(data)
        if not 1 <= valid_bytes <= QROS_BLOCK_SIZE - 1:
            raise ValueError(
                f"Čiastočný blok musí mať 1–{QROS_BLOCK_SIZE - 1} bajtov, "
                f"má {valid_bytes}")
        block += bytes(data)
        block += b'\x00' * (QROS_BLOCK_SIZE - valid_bytes - 1)
        block.append(valid_bytes)
    elif block_type == QROS_BLOCK_EOF:
        # EOF blok — nulové dáta, signalizuje koniec súboru
        block += b'\x00' * QROS_BLOCK_SIZE
    else:
        raise ValueError(f"Neznámy typ bloku: ${block_type:02X}")

    block.append(sio_checksum(block))
    return block


# =============================================================================
# Sekvencia záznamov — spoločný zdroj pravdy pre CAS aj WAV
# =============================================================================

def iter_qros_records(raw_data, name, leading_blank=False):
    """
    Generuje celú kazetovú sekvenciu ako (blok, irg_ms, druh).

    Poradie: [nastavovací blok] → hlavička → hlavička → dáta… → EOF.
    Túto funkciu používa CAS aj WAV cesta, takže sa nemôžu rozísť.
    """
    irg = QROS_IRG_HEADER

    if leading_blank:
        yield make_qros_blank_block(), irg, 'blank'
        irg = QROS_IRG_HEADER_REPEAT

    header = make_qros_header(name)
    yield header, irg, 'header'
    yield header, QROS_IRG_HEADER_REPEAT, 'header'

    index = QROS_FIRST_DATA_INDEX
    pos = 0
    total = len(raw_data)

    while pos < total:
        chunk = raw_data[pos:pos + QROS_BLOCK_SIZE]
        if len(chunk) == QROS_BLOCK_SIZE:
            block = make_qros_data_block(index, chunk, QROS_BLOCK_COMPLETE)
        else:
            block = make_qros_data_block(index, chunk, QROS_BLOCK_PARTIAL)
        yield block, QROS_IRG_DATA, 'data'
        pos += len(chunk)
        index += 1

    yield make_qros_data_block(index, b'', QROS_BLOCK_EOF), QROS_IRG_DATA, 'eof'


def qros_plan(size, leading_blank=False):
    """
    Vypočíta zloženie pásky bez toho, aby sa bloky naozaj vyrábali.
    Vracia slovník s počtami blokov a celkovým počtom záznamov.
    """
    complete = size // QROS_BLOCK_SIZE
    partial = 1 if size % QROS_BLOCK_SIZE else 0
    records = 2 + complete + partial + 1 + (1 if leading_blank else 0)
    return {
        'blocks_complete': complete,
        'blocks_partial': partial,
        'blocks_eof': 1,
        'blocks_header': 2,
        'blocks_blank': 1 if leading_blank else 0,
        'records': records,
    }


def qros_tape_duration(size, baudrate, leading_blank=False, tail_ms=0,
                       boot_records=0):
    """Odhad dĺžky nahrávky v sekundách (IRG + prenos + koncové ticho)."""
    records = qros_plan(size, leading_blank)['records']
    irg_ms = QROS_IRG_HEADER + QROS_IRG_HEADER_REPEAT * (records - 1)
    bits = records * QROS_RECORD_SIZE * QROS_FRAME_BITS
    seconds = (irg_ms + tail_ms) / 1000.0 + bits / float(baudrate)

    if boot_records:                    # zavádzač ide pomalou štandardnou rýchlosťou
        boot_irg = BOOT_IRG_FIRST + BOOT_IRG_NEXT * (boot_records - 1)
        boot_bits = boot_records * QROS_RECORD_SIZE * QROS_FRAME_BITS
        seconds += boot_irg / 1000.0 + boot_bits / float(CAS_BOOT_BAUD)
    return seconds


def format_duration(seconds):
    """Naformátuje sekundy ako 'M:SS' alebo 'H:MM:SS'."""
    seconds = int(round(seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def format_size(num_bytes):
    """Naformátuje veľkosť v B / kB / MB."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.1f} kB"
    return f"{num_bytes / (1024 * 1024):.1f} MB"


# =============================================================================
# Konverzia XEX -> QROS CAS
# =============================================================================

def build_cas(raw_data, name, baudrate=QROS_DEFAULT_BAUD,
              leading_blank=False, progress=None, boot=False, loader=None):
    """
    Zostaví CAS obraz zo surových bajtov súboru.
    `progress(hotovo, celkom)` sa volá po každom zázname (voliteľné).
    Pri boot=True sa pred QROS dáta vloží zavádzač v štandardných
    600-baud záznamoch, takže sa páska spustí sama (START pri zapnutí).
    """
    plan = qros_plan(len(raw_data), leading_blank)
    total = plan['records']

    cas = QROSCASWriter()
    cas.add_fuji(name)

    boot_records = []
    if boot:
        blob = loader if loader is not None else load_boot_loader()
        boot_records = build_boot_records(
            blob,
            total_blocks=plan['blocks_complete'] + plan['blocks_partial'],
            divisor=pokey_divisor(baudrate))
        cas.add_baud(CAS_BOOT_BAUD)
        for i, record in enumerate(boot_records):
            cas.add_data(record, BOOT_IRG_FIRST if i == 0 else BOOT_IRG_NEXT)

    cas.add_baud(baudrate)

    for done, (block, irg, _kind) in enumerate(
            iter_qros_records(raw_data, name, leading_blank), start=1):
        cas.add_data(block, irg)
        if progress:
            progress(done, total)

    info = dict(plan)
    info.update({
        'data_size': len(raw_data),
        'baudrate': baudrate,
        'name': name,
        'cas_size': len(cas),
        'boot_records': len(boot_records),
        'tape_duration_s': qros_tape_duration(
            len(raw_data), baudrate, leading_blank,
            boot_records=len(boot_records)),
    })
    return cas, info


def convert_xex_to_qros(xex, tape_name=None, baudrate=None,
                        leading_blank=False, progress=None, boot=False):
    """
    Konvertuje XEX súbor do QROS/EMO CAS formátu.
    QROS nahráva surové dáta súboru (nie parsované XEX segmenty).
    """
    if baudrate is None:
        baudrate = QROS_DEFAULT_BAUD
    name = tape_name or xex.default_tape_name()
    return build_cas(xex.get_raw_file_data(), name, baudrate,
                     leading_blank, progress, boot=boot)


# =============================================================================
# WAV generátor — Manchester kódovanie pre QROS/EMO
# =============================================================================

class ManchesterEncoder:
    """
    Manchester (biphase-L) kodér s fázovým akumulátorom.

    SIO rámec: 1 štart bit (0) + 8 dátových bitov (LSB first) + 1 stop bit (1).
    Každý bit má povinný prechod v strede intervalu:
        bit 0 = prvá polovica HIGH, druhá LOW
        bit 1 = prvá polovica LOW,  druhá HIGH
    (invert=True celý priebeh otočí).

    Fázový akumulátor prenáša zvyšok vzoriek medzi bitmi, takže pri
    neceločíselnom samples_per_bit (96000/6595 = 14.5565) nevzniká drift.
    Vzorky sa pridávajú blokovo cez array * n — natívna C operácia,
    rádovo rýchlejšia ako cyklus po vzorkách.
    """

    def __init__(self, baudrate, sample_rate=WAV_SAMPLE_RATE,
                 amplitude=WAV_AMPLITUDE, invert=False):
        if baudrate <= 0:
            raise ValueError("Prenosová rýchlosť musí byť kladná")
        self.baudrate = baudrate
        self.sample_rate = sample_rate
        self.amplitude = max(1, min(32767, int(amplitude)))
        self.invert = invert
        self.samples_per_bit = sample_rate / float(baudrate)
        if self.samples_per_bit < 4:
            raise ValueError(
                f"Vzorkovanie {sample_rate} Hz je pre {baudrate} baud príliš nízke "
                f"({self.samples_per_bit:.1f} vzoriek na bit)")

        amp = self.amplitude
        sign = -1 if invert else 1
        # levels[bit] = (úroveň prvej polovice, úroveň druhej polovice)
        self.levels = (
            (sign * amp, -sign * amp),   # bit 0
            (-sign * amp, sign * amp),   # bit 1
        )
        # Predpripravené jednovzorkové polia pre blokové násobenie
        self._unit = {
            amp: array.array('h', [amp]),
            -amp: array.array('h', [-amp]),
        }

    def encode_block(self, data):
        """Zakóduje postupnosť bajtov do vzoriek (array 'h')."""
        spb = self.samples_per_bit
        half = spb / 2.0
        levels = self.levels
        unit = self._unit
        result = array.array('h')
        phase_acc = 0.0

        for byte_val in data:
            # SIO rámec: štart(0) + 8 dátových bitov LSB first + stop(1)
            bits = (0,
                    byte_val & 1, (byte_val >> 1) & 1, (byte_val >> 2) & 1,
                    (byte_val >> 3) & 1, (byte_val >> 4) & 1, (byte_val >> 5) & 1,
                    (byte_val >> 6) & 1, (byte_val >> 7) & 1,
                    1)
            for bit in bits:
                first_val, second_val = levels[bit]
                total_needed = phase_acc + spb
                n_samples = int(round(total_needed))
                n_first = int(round(phase_acc + half)) - int(round(phase_acc))
                n_second = n_samples - n_first
                result.extend(unit[first_val] * n_first)
                result.extend(unit[second_val] * n_second)
                phase_acc = total_needed - n_samples

        return result

    def silence(self, duration_ms):
        """Vygeneruje ticho zadanej dĺžky v milisekundách."""
        n_samples = int(self.sample_rate * duration_ms / 1000.0)
        return array.array('h', [0]) * n_samples


def _samples_to_frames(samples):
    """Prevedie array('h') na little-endian bajty (bezpečné aj na big-endian CPU)."""
    if sys.byteorder == 'big':
        samples = array.array('h', samples)
        samples.byteswap()
    return samples.tobytes()


def write_qros_wav(filename, raw_data, name, baudrate=QROS_DEFAULT_BAUD,
                   sample_rate=WAV_SAMPLE_RATE, amplitude=WAV_AMPLITUDE,
                   invert=False, leading_blank=False, tail_ms=WAV_TAIL_MS,
                   progress=None):
    """
    Zapíše WAV nahrávku priamo na disk, blok po bloku.

    Na rozdiel od v0.4 sa celá nahrávka nedrží v pamäti — pri veľkom XEX
    to bola desiatka až stovky MB. Štruktúra je zhodná s CAS výstupom,
    lebo obe cesty čítajú z iter_qros_records().
    """
    enc = ManchesterEncoder(baudrate, sample_rate, amplitude, invert)
    plan = qros_plan(len(raw_data), leading_blank)
    total = plan['records']
    n_samples = 0

    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)

        for done, (block, irg, _kind) in enumerate(
                iter_qros_records(raw_data, name, leading_blank), start=1):
            gap = enc.silence(irg)
            signal = enc.encode_block(block)
            wf.writeframes(_samples_to_frames(gap))
            wf.writeframes(_samples_to_frames(signal))
            n_samples += len(gap) + len(signal)
            if progress:
                progress(done, total)

        if tail_ms:
            tail = enc.silence(tail_ms)
            wf.writeframes(_samples_to_frames(tail))
            n_samples += len(tail)

    info = dict(plan)
    info.update({
        'data_size': len(raw_data),
        'baudrate': baudrate,
        'name': name,
        'sample_rate': sample_rate,
        'amplitude': enc.amplitude,
        'invert': invert,
        'wav_samples': n_samples,
        'wav_duration_s': n_samples / float(sample_rate),
    })
    return info


def convert_qros_to_wav(xex, tape_name=None, baudrate=None, invert=False,
                        sample_rate=WAV_SAMPLE_RATE, amplitude=WAV_AMPLITUDE,
                        leading_blank=False, tail_ms=WAV_TAIL_MS):
    """
    Vytvorí WAV vzorky v pamäti (kompatibilita s v0.4 API).
    Pre zápis na disk je úspornejšie write_qros_wav().
    """
    if baudrate is None:
        baudrate = QROS_DEFAULT_BAUD
    name = tape_name or xex.default_tape_name()
    raw_data = xex.get_raw_file_data()

    enc = ManchesterEncoder(baudrate, sample_rate, amplitude, invert)
    samples = array.array('h')
    for block, irg, _kind in iter_qros_records(raw_data, name, leading_blank):
        samples.extend(enc.silence(irg))
        samples.extend(enc.encode_block(block))
    if tail_ms:
        samples.extend(enc.silence(tail_ms))

    info = dict(qros_plan(len(raw_data), leading_blank))
    info.update({
        'data_size': len(raw_data),
        'baudrate': baudrate,
        'name': name,
        'sample_rate': sample_rate,
        'wav_samples': len(samples),
        'wav_duration_s': len(samples) / float(sample_rate),
    })
    return samples, info


def write_wav(filename, samples, sample_rate=WAV_SAMPLE_RATE):
    """Zapíše vzorky do WAV súboru (16-bit mono PCM)."""
    with wave.open(filename, 'wb') as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(_samples_to_frames(samples))


# =============================================================================
# Overenie výstupu — spätná kontrola CAS a WAV
# =============================================================================

def blocks_to_payload(blocks):
    """
    Poskladá pôvodné dáta zo sekvencie QROS blokov.
    Vracia (dáta, chyby).
    """
    payload = bytearray()
    errors = []
    seen_eof = False

    for i, block in enumerate(blocks):
        if len(block) != QROS_RECORD_SIZE:
            errors.append(f"Blok #{i} má {len(block)} bajtov namiesto {QROS_RECORD_SIZE}")
            continue
        if sio_checksum(block[:-1]) != block[-1]:
            errors.append(
                f"Blok #{i} (index {block[0] << 8 | block[1]}): zlý checksum "
                f"(${block[-1]:02X} != ${sio_checksum(block[:-1]):02X})")
        btype = block[2]
        if seen_eof:
            errors.append(f"Blok #{i} nasleduje až za EOF blokom")
        if btype == QROS_BLOCK_COMPLETE:
            payload += block[3:131]
        elif btype == QROS_BLOCK_PARTIAL:
            count = block[130]
            if not 1 <= count <= QROS_BLOCK_SIZE - 1:
                errors.append(f"Blok #{i}: neplatný počet platných bajtov {count}")
            else:
                payload += block[3:3 + count]
        elif btype == QROS_BLOCK_EOF:
            seen_eof = True
        elif btype in (QROS_BLOCK_HEADER, QROS_BLOCK_BLANK):
            pass
        else:
            errors.append(f"Blok #{i}: neznámy typ ${btype:02X}")

    if not seen_eof:
        errors.append("Chýba koncový blok ($FE)")
    return bytes(payload), errors


def verify_cas(cas_bytes, expected_raw=None, expected_baud=None):
    """
    Skontroluje vygenerovaný CAS: štruktúru chunkov, indexy, typy,
    checksumy a (ak je zadané) zhodu dát s pôvodným súborom.
    Vracia (ok, správy) — ok je True, len ak nie je nájdený žiadny problém.
    """
    problems = []
    notes = []

    try:
        chunks = parse_cas(cas_bytes)
    except ValueError as e:
        return False, [f"Poškodená štruktúra CAS: {e}"]

    if not chunks or chunks[0][0] != b'FUJI':
        problems.append("Prvý chunk nie je FUJI")

    baud_chunks = [c for c in chunks if c[0] == b'baud']
    if not baud_chunks:
        problems.append("Chýba baud chunk")
    elif expected_baud is not None and not any(
            c[1] == expected_baud for c in baud_chunks):
        problems.append(
            f"medzi baud chunkami chýba {expected_baud} "
            f"(sú tam {[c[1] for c in baud_chunks]})")

    for cid in sorted({c[0] for c in chunks} - {b'FUJI', b'baud', b'data'}):
        problems.append(f"Neznámy chunk {cid.decode('latin1')}")

    all_data = [c for c in chunks if c[0] == b'data']
    if not all_data:
        return False, problems + ["CAS neobsahuje žiadne dátové bloky"]

    # Bootovacie záznamy (55 55 ...) sa kontrolujú zvlášť — nemajú index bloku
    boot_chunks = [c for c in all_data if is_boot_record(c[2])]
    data_chunks = [c for c in all_data if not is_boot_record(c[2])]

    for i, c in enumerate(boot_chunks):
        record = c[2]
        if sio_checksum(record[:-1]) != record[-1]:
            problems.append(f"Bootovací záznam #{i}: zlý checksum")
    if boot_chunks:
        notes.append(f"Bootovací zavádzač: {len(boot_chunks)} záznamov")
        if not any(c[1] == CAS_BOOT_BAUD for c in baud_chunks):
            problems.append(f"Chýba baud chunk {CAS_BOOT_BAUD} pre zavádzač")

    if not data_chunks:
        return False, problems + ["CAS neobsahuje žiadne QROS bloky"]

    blocks = [c[2] for c in data_chunks]

    # Indexy: nastavovací blok 0, hlavičky 1, dáta vzostupne od 2
    expected_index = QROS_FIRST_DATA_INDEX
    for i, block in enumerate(blocks):
        if len(block) != QROS_RECORD_SIZE:
            continue
        index = block[0] << 8 | block[1]
        btype = block[2]
        if btype == QROS_BLOCK_HEADER:
            if index != QROS_HEADER_INDEX:
                problems.append(
                    f"Hlavička #{i} má index {index} namiesto {QROS_HEADER_INDEX}")
        elif btype == QROS_BLOCK_BLANK:
            if index != 0:
                problems.append(
                    f"Nastavovací blok #{i} má index {index} namiesto 0")
        else:
            if index != expected_index:
                problems.append(
                    f"Blok #{i} má index {index}, očakávaný {expected_index}")
            expected_index = index + 1

    # IRG medzery
    if data_chunks[0][1] != QROS_IRG_HEADER:
        problems.append(
            f"Prvý IRG je {data_chunks[0][1]} ms namiesto {QROS_IRG_HEADER} ms")
    for i, c in enumerate(data_chunks[1:], start=1):
        if c[1] != QROS_IRG_DATA:
            problems.append(
                f"IRG bloku #{i} je {c[1]} ms namiesto {QROS_IRG_DATA} ms")

    payload, errors = blocks_to_payload(blocks)
    problems.extend(errors)

    if expected_raw is not None:
        expected = bytes(expected_raw)
        if payload == expected:
            notes.append(f"Dáta sedia bit-for-bit ({len(payload)} B)")
        elif len(payload) != len(expected):
            problems.append(
                f"Spätne zložené dáta majú {len(payload)} B, "
                f"pôvodný súbor {len(expected)} B")
        else:
            diff = next(k for k, (a, b) in enumerate(zip(payload, expected))
                        if a != b)
            problems.append(f"Dáta sa líšia od pôvodného súboru na offsete {diff}")

    return not problems, problems + notes


def decode_manchester_wav(samples, sample_rate, baudrate, record_size=QROS_RECORD_SIZE):
    """
    Spätne dekóduje QROS WAV vzorky na 132-bajtové bloky.

    Bloky sú oddelené tichom (IRG), vnútri bloku signál nikdy nie je nulový,
    takže stačí nájsť začiatok signálu a odtiaľ dekódovať pevný počet rámcov
    rovnakým fázovým akumulátorom, aký použil kodér.
    Polarita sa zistí zo štart bitu prvého rámca.

    Vracia (bloky, chyby).
    """
    spb = sample_rate / float(baudrate)
    n = len(samples)
    errors = []
    blocks = []

    peak = 0
    for i in range(0, n, 97):          # hrubý odhad amplitúdy (nepárny krok)
        v = samples[i]
        if v < 0:
            v = -v
        if v > peak:
            peak = v
    if peak == 0:
        return [], ["WAV neobsahuje žiadny signál"]
    thr = peak // 3

    step = max(1, int(spb // 4))
    bits_per_block = record_size * QROS_FRAME_BITS
    pos = 0

    while pos < n:
        v = samples[pos]
        if (v if v >= 0 else -v) <= thr:
            pos += step
            continue

        # Spresni začiatok bloku dozadu na prvú vzorku nad prahom
        start = pos
        while start > 0 and abs(samples[start - 1]) > thr:
            start -= 1

        # Polarita: štart bit je vždy 0 → prvá polovica má úroveň bitu 0
        polarity = 1 if samples[start] > 0 else -1

        bits = []
        phase_acc = 0.0
        cursor = float(start)
        ok = True
        for _ in range(bits_per_block):
            base = int(round(cursor))
            n_samples = int(round(phase_acc + spb))
            first_idx = base + int(spb * 0.25)
            second_idx = base + int(spb * 0.75)
            if second_idx >= n:
                ok = False
                break
            first = samples[first_idx] * polarity
            second = samples[second_idx] * polarity
            if first > 0 and second < 0:
                bits.append(0)
            elif first < 0 and second > 0:
                bits.append(1)
            else:
                ok = False
                break
            cursor += n_samples
            phase_acc = phase_acc + spb - n_samples

        if not ok:
            errors.append(f"Nečitateľný Manchester signál od vzorky {start}")
            pos = start + int(spb)
            continue

        block = bytearray()
        for f in range(record_size):
            frame = bits[f * QROS_FRAME_BITS:(f + 1) * QROS_FRAME_BITS]
            if frame[0] != 0:
                errors.append(f"Blok od vzorky {start}: zlý štart bit v rámci {f}")
                ok = False
                break
            if frame[9] != 1:
                errors.append(f"Blok od vzorky {start}: zlý stop bit v rámci {f}")
                ok = False
                break
            value = 0
            for b in range(8):
                value |= frame[1 + b] << b
            block.append(value)

        if ok:
            blocks.append(bytes(block))
        # Za blokom vždy nasleduje IRG ticho — preskočíme skutočný koniec
        # bloku (podľa kurzora) plus rezervu jedného bitu.
        pos = int(round(cursor)) + int(spb) + 1

    return blocks, errors


def verify_wav(filename, expected_raw, baudrate):
    """
    Prehrá vygenerovaný WAV späť cez dekodér a porovná dáta s originálom.
    Vracia (ok, správy).
    """
    msgs = []
    with wave.open(filename, 'rb') as wf:
        if wf.getnchannels() != 1 or wf.getsampwidth() != 2:
            return False, ["WAV nie je 16-bit mono"]
        sample_rate = wf.getframerate()
        frames = wf.readframes(wf.getnframes())
    samples = array.array('h')
    samples.frombytes(frames)
    if sys.byteorder == 'big':
        samples.byteswap()

    blocks, errors = decode_manchester_wav(samples, sample_rate, baudrate)
    msgs.extend(errors)
    if not blocks:
        return False, msgs + ["Z WAV sa nepodarilo dekódovať žiadny blok"]

    payload, block_errors = blocks_to_payload(blocks)
    msgs.extend(block_errors)

    if payload == bytes(expected_raw):
        msgs.append(f"WAV dekódovaný späť bit-for-bit ({len(blocks)} blokov)")
    else:
        msgs.append(
            f"Dekódované dáta ({len(payload)} B) sa nezhodujú "
            f"s originálom ({len(expected_raw)} B)")

    ok = not errors and not block_errors and payload == bytes(expected_raw)
    return ok, msgs


# =============================================================================
# Konverzná úloha (spoločná pre GUI aj CLI)
# =============================================================================

class ConversionOptions:
    """Nastavenia jednej konverzie."""

    def __init__(self, baudrate=QROS_DEFAULT_BAUD, make_wav=False, wav_only=False,
                 sample_rate=WAV_SAMPLE_RATE, amplitude=WAV_AMPLITUDE,
                 invert=False, leading_blank=False, verify=True,
                 verify_wav_data=False, tape_name=None, boot=False):
        self.baudrate = baudrate
        self.make_wav = make_wav or wav_only
        self.wav_only = wav_only
        self.sample_rate = sample_rate
        self.amplitude = amplitude
        self.invert = invert
        self.leading_blank = leading_blank
        self.verify = verify
        self.verify_wav_data = verify_wav_data
        self.tape_name = tape_name
        self.boot = boot


def convert_file(input_path, output_path, opts, log=print, progress=None):
    """
    Skonvertuje jeden XEX na CAS (a voliteľne WAV), voliteľne overí výstup.
    Vracia slovník s výsledkom. Nezachytáva výnimky — rieši ich volajúci.
    """
    xex = XEXFile(input_path)
    raw = xex.get_raw_file_data()
    name = opts.tape_name or xex.default_tape_name()

    out_dir = os.path.dirname(os.path.abspath(output_path))
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)

    display_name, truncated = to_atascii(name)
    if truncated:
        log(f"Názov skrátený na {QROS_NAME_LENGTH} znakov: "
            f"{display_name.decode('latin1').rstrip()}", 'warn')

    for w in xex.warnings:
        log(f"XEX: {w}", 'warn')

    result = {'input': input_path, 'name': name}

    if not opts.wav_only:
        cas, info = convert_xex_to_qros(xex, name, opts.baudrate,
                                        opts.leading_blank, progress,
                                        boot=opts.boot)
        cas.write(output_path)
        result['cas_path'] = output_path
        result['cas_size'] = len(cas)
        result['info'] = info
        boot_note = (f", zavádzač {info['boot_records']} zázn."
                     if info.get('boot_records') else "")
        log(f"CAS: {os.path.basename(output_path)} "
            f"({format_size(len(cas))}, {info['records']} záznamov{boot_note}, "
            f"~{format_duration(info['tape_duration_s'])})", 'ok')

        if opts.verify:
            ok, msgs = verify_cas(cas.chunks, raw, opts.baudrate)
            for m in msgs:
                log(f"  overenie CAS: {m}", 'ok' if ok else 'err')
            result['cas_ok'] = ok
    else:
        result['info'] = dict(qros_plan(len(raw), opts.leading_blank))
        result['info'].update({'data_size': len(raw), 'baudrate': opts.baudrate,
                               'name': name})

    if opts.make_wav:
        wav_path = os.path.splitext(output_path)[0] + ".wav"
        wav_info = write_qros_wav(
            wav_path, raw, name, baudrate=opts.baudrate,
            sample_rate=opts.sample_rate, amplitude=opts.amplitude,
            invert=opts.invert, leading_blank=opts.leading_blank,
            progress=progress)
        wav_size = os.path.getsize(wav_path)
        result['wav_path'] = wav_path
        result['wav_size'] = wav_size
        result['wav_info'] = wav_info
        log(f"WAV: {os.path.basename(wav_path)} "
            f"({format_size(wav_size)}, {wav_info['wav_duration_s']:.1f} s, "
            f"{opts.sample_rate} Hz)", 'ok')

        if opts.verify and opts.verify_wav_data:
            ok, msgs = verify_wav(wav_path, raw, opts.baudrate)
            for m in msgs:
                log(f"  overenie WAV: {m}", 'ok' if ok else 'err')
            result['wav_ok'] = ok

    return result


# =============================================================================
# Príkazový riadok
# =============================================================================

def build_arg_parser():
    p = argparse.ArgumentParser(
        prog="qros_generator",
        description=f"{APP_NAME} v{APP_VERSION} - XEX/COM/OBX -> QROS/EMO CAS a WAV. "
                    "Bez argumentov sa spustí grafické rozhranie.")
    p.add_argument('input', nargs='*',
                   help="vstupné súbory XEX/COM/OBX (viac súborov = dávka)")
    p.add_argument('-o', '--output',
                   help="výstupný CAS (len pri jednom vstupnom súbore)")
    p.add_argument('-n', '--name',
                   help=f"názov na páske (max {QROS_NAME_LENGTH} znakov)")
    p.add_argument('-b', '--baud', type=int, default=QROS_DEFAULT_BAUD,
                   help=f"prenosová rýchlosť (predvolene {QROS_DEFAULT_BAUD})")
    p.add_argument('-w', '--wav', action='store_true',
                   help="vygenerovať aj WAV")
    p.add_argument('--wav-only', action='store_true',
                   help="vygenerovať len WAV (bez CAS)")
    p.add_argument('--rate', type=int, default=WAV_SAMPLE_RATE,
                   help=f"vzorkovanie WAV v Hz (predvolene {WAV_SAMPLE_RATE})")
    p.add_argument('--volume', type=int, default=61,
                   help="hlasitosť WAV v %% z rozsahu 16-bit (predvolene 61)")
    p.add_argument('--invert', action='store_true',
                   help="invertovaná polarita WAV signálu")
    p.add_argument('--blank', action='store_true',
                   help="pridať úvodný nastavovací blok ($FF)")
    p.add_argument('--boot', action='store_true',
                   help="bootovacia páska: pred dáta sa vloží zavádzač "
                        "(v emulátore stačí START pri zapnutí)")
    p.add_argument('--rename', metavar='NAZOV',
                   help="prepísať názov v hotovom CAS súbore a skončiť")
    p.add_argument('--no-verify', action='store_true',
                   help="preskočiť spätné overenie výstupu")
    p.add_argument('--verify-wav', action='store_true',
                   help="overiť aj WAV spätným dekódovaním (pomalšie)")
    p.add_argument('--info', action='store_true',
                   help="len vypísať analýzu vstupu, nekonvertovať")
    p.add_argument('-V', '--version', action='version',
                   version=f"{APP_NAME} {APP_VERSION}")
    return p


def _prepare_console():
    """
    Windows konzola beží v cp1250 — znaky ako '→' by zhodili výpis
    na UnicodeEncodeError. Nahradíme ich namiesto pádu.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors='replace')
        except (AttributeError, ValueError):
            pass


def cli_log(msg, tag=None):
    prefix = {'err': "CHYBA: ", 'warn': "POZOR: "}.get(tag, "")
    stream = sys.stderr if tag == 'err' else sys.stdout
    print(prefix + msg, file=stream)


def print_xex_info(xex):
    print(f"Súbor:    {xex.filename} ({format_size(xex.size)})")
    print(f"Segmenty: {len(xex.segments)}")
    for seg in xex.segments:
        print(f"   {seg}")
    if xex.run_address is not None:
        print(f"RUN:      ${xex.run_address:04X}")
    for addr in xex.init_addresses:
        print(f"INIT:     ${addr:04X}")
    for w in xex.warnings:
        cli_log(w, 'warn')
    plan = qros_plan(xex.size)
    print(f"Bloky:    {plan['blocks_complete']} úplných + "
          f"{plan['blocks_partial']} neúplných + EOF "
          f"({plan['records']} záznamov)")
    for baud, note in QROS_BAUD_CHOICES:
        dur = qros_tape_duration(xex.size, baud)
        print(f"  {baud:>5} baud -> {format_duration(dur):>6}  ({note})")


def cli_rename(args):
    """Prepíše názov v hotových CAS obrazoch (pásky z iných generátorov)."""
    failed = 0
    for path in args.input:
        try:
            with open(path, 'rb') as f:
                data = f.read()
            new_data, changed = rename_cas(data, args.rename)
            if not changed:
                cli_log(f"{os.path.basename(path)}: nenašiel sa hlavičkový blok",
                        'warn')
            out = args.output or path
            with open(out, 'wb') as f:
                f.write(new_data)
            name_bytes, truncated = to_atascii(args.rename)
            shown = name_bytes.decode('latin1').rstrip()
            print(f"{os.path.basename(out)}: názov na páske = {shown!r} "
                  f"({changed} hlavičiek)")
            if truncated:
                cli_log(f"názov skrátený na {QROS_NAME_LENGTH} znakov", 'warn')
        except Exception as e:
            cli_log(f"{os.path.basename(path)}: {e}", 'err')
            failed += 1
    return 1 if failed else 0


def main_cli(argv):
    _prepare_console()
    args = build_arg_parser().parse_args(argv)

    if not args.input:
        return None                     # spustí sa GUI

    if args.output and len(args.input) > 1:
        cli_log("--output sa dá použiť len s jedným vstupným súborom", 'err')
        return 2

    if args.rename:
        return cli_rename(args)

    amplitude = int(32767 * max(1, min(100, args.volume)) / 100)
    opts = ConversionOptions(
        baudrate=args.baud, make_wav=args.wav, wav_only=args.wav_only,
        sample_rate=args.rate, amplitude=amplitude, invert=args.invert,
        leading_blank=args.blank, verify=not args.no_verify,
        verify_wav_data=args.verify_wav, tape_name=args.name,
        boot=args.boot)

    failed = 0
    for path in args.input:
        try:
            if args.info:
                print_xex_info(XEXFile(path))
                print()
                continue
            out = args.output or os.path.splitext(path)[0] + ".cas"
            print(f"-- {os.path.basename(path)}")
            result = convert_file(path, out, opts, log=cli_log)
            if result.get('cas_ok') is False or result.get('wav_ok') is False:
                failed += 1
        except Exception as e:
            cli_log(f"{os.path.basename(path)}: {e}", 'err')
            failed += 1

    return 1 if failed else 0


# =============================================================================
# Farebná schéma GUI
# =============================================================================

COLORS = {
    'bg': '#f0f0f0',
    'bg2': '#ffffff',
    'bg3': '#d8dce6',
    'accent': '#2860a8',
    'accent2': '#3a78cc',
    'text': '#1a1a1a',
    'text2': '#606880',
    'entry_bg': '#ffffff',
    'entry_fg': '#1a1a1a',
    'ok': '#1a8a3a',
    'warn': '#b87800',
    'title_bg': '#2860a8',
    'title_fg': '#ffffff',
    'btn_fg': '#ffffff',
    'section': '#3868a0',
    'log_bg': '#fafcff',
}


# =============================================================================
# Grafické rozhranie (GUI)
# =============================================================================

class XEX2CASApp:
    """Hlavná trieda aplikácie s tkinter GUI."""

    def __init__(self):
        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} v{APP_VERSION}")
        self.root.configure(bg=COLORS['bg'])
        self.root.resizable(True, True)
        self.root.minsize(760, 640)

        self.xex = None
        self.busy = False
        self._build_gui()
        self._center_window(820, 700)
        self._update_estimate()

    def _center_window(self, w, h):
        """Vycentruje okno na obrazovke."""
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        self.root.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+{max(0, (sh - h) // 2)}")

    # ---- Vlastné štýlované widgety ----

    def _make_label(self, parent, text, size=10, bold=False, color=None):
        font = ("Segoe UI", size, "bold" if bold else "normal")
        return tk.Label(parent, text=text, font=font,
                        bg=parent['bg'], fg=color or COLORS['text'])

    def _make_entry(self, parent, textvariable=None, width=40):
        return tk.Entry(parent, textvariable=textvariable, width=width,
                        font=("Consolas", 10),
                        bg=COLORS['entry_bg'], fg=COLORS['entry_fg'],
                        insertbackground=COLORS['accent'],
                        relief=tk.GROOVE, bd=2,
                        selectbackground=COLORS['accent'],
                        selectforeground='#fff')

    def _make_button(self, parent, text, command, accent=False):
        bg = COLORS['accent'] if accent else COLORS['bg3']
        fg = COLORS['btn_fg'] if accent else COLORS['text']
        hover = COLORS['accent2'] if accent else '#c0c8d8'
        btn = tk.Button(parent, text=text, command=command,
                        font=("Segoe UI", 10, "bold" if accent else "normal"),
                        bg=bg, fg=fg, activebackground=hover,
                        activeforeground=fg, relief=tk.FLAT, bd=0,
                        padx=16, pady=6, cursor='hand2')
        btn.bind('<Enter>', lambda e: btn.config(bg=hover)
                 if str(btn['state']) != 'disabled' else None)
        btn.bind('<Leave>', lambda e: btn.config(bg=bg))
        return btn

    def _make_check(self, parent, text, variable, command=None):
        return tk.Checkbutton(parent, text=text, variable=variable,
                              command=command, font=("Segoe UI", 9),
                              bg=COLORS['bg2'], fg=COLORS['text'],
                              selectcolor=COLORS['bg2'],
                              activebackground=COLORS['bg2'],
                              activeforeground=COLORS['accent'])

    def _make_frame(self, parent, label=None):
        frm = tk.Frame(parent, bg=COLORS['bg2'], bd=1,
                       relief=tk.GROOVE, highlightthickness=0)
        frm.pack(fill=tk.X, padx=12, pady=5)
        if label:
            self._make_label(frm, label, size=9, bold=True,
                             color=COLORS['section']).pack(anchor=tk.W,
                                                           padx=8, pady=(8, 2))
        return frm

    # ---- Zostavenie GUI ----

    def _build_gui(self):
        # Titulný pruh
        frm_title = tk.Frame(self.root, bg=COLORS['title_bg'], height=50)
        frm_title.pack(fill=tk.X)
        frm_title.pack_propagate(False)
        tk.Label(frm_title, text=f"  {APP_NAME}",
                 font=("Consolas", 18, "bold"),
                 bg=COLORS['title_bg'], fg=COLORS['title_fg']).pack(
            side=tk.LEFT, padx=(12, 4))
        tk.Label(frm_title, text="XEX → CAS / WAV",
                 font=("Segoe UI", 12),
                 bg=COLORS['title_bg'], fg='#a0c8ff').pack(
            side=tk.LEFT, padx=4)
        tk.Label(frm_title, text="QROS: Ing. M. Žúbor  HW 2025: Aleister  Gen: W1K  ",
                 font=("Segoe UI", 9),
                 bg=COLORS['title_bg'], fg='#c0d8ff').pack(
            side=tk.RIGHT, padx=12)

        # --- Záložky (Notebook) ---
        style = ttk.Style()
        style.configure('TNotebook', background=COLORS['bg'])
        style.configure('TNotebook.Tab', font=("Segoe UI", 10, "bold"),
                        padding=[12, 4])

        self.notebook = ttk.Notebook(self.root)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=8, pady=(4, 8))

        tab_convert = tk.Frame(self.notebook, bg=COLORS['bg'])
        self.notebook.add(tab_convert, text="  Konverzia  ")
        self._build_convert_tab(tab_convert)

        tab_info = tk.Frame(self.notebook, bg=COLORS['bg'])
        self.notebook.add(tab_info, text="  Info  ")
        self._build_info_tab(tab_info)

        tab_changelog = tk.Frame(self.notebook, bg=COLORS['bg'])
        self.notebook.add(tab_changelog, text="  Changelog  ")
        self._build_changelog_tab(tab_changelog)

    def _build_convert_tab(self, tab_convert):
        # --- Vstupný súbor ---
        frm = self._make_frame(tab_convert, "VSTUPNÝ SÚBOR  (XEX / COM / OBX)")
        row = tk.Frame(frm, bg=COLORS['bg2'])
        row.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.var_input = tk.StringVar()
        self._make_entry(row, self.var_input, 50).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self._make_button(row, "Vybrať...", self._browse_input).pack(
            side=tk.RIGHT)

        # --- Výstupný súbor ---
        frm = self._make_frame(tab_convert, "VÝSTUPNÝ SÚBOR  (CAS)")
        row = tk.Frame(frm, bg=COLORS['bg2'])
        row.pack(fill=tk.X, padx=8, pady=(0, 8))
        self.var_output = tk.StringVar()
        self._make_entry(row, self.var_output, 50).pack(
            side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 6))
        self._make_button(row, "Vybrať...", self._browse_output).pack(
            side=tk.RIGHT)

        # --- Nastavenia ---
        frm = self._make_frame(tab_convert, "NASTAVENIA")
        grid = tk.Frame(frm, bg=COLORS['bg2'])
        grid.pack(fill=tk.X, padx=8, pady=(0, 8))

        # Názov na páske
        self._make_label(grid, "Názov na páske:", size=9).grid(
            row=0, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        self.var_name = tk.StringVar()
        self._make_entry(grid, self.var_name, 26).grid(
            row=0, column=1, sticky=tk.W, pady=3)
        self._make_label(grid, f"(max {QROS_NAME_LENGTH} znakov, ATASCII bez diakritiky)",
                         size=8, color=COLORS['text2']).grid(
            row=0, column=2, sticky=tk.W, padx=8, pady=3)

        # Prenosová rýchlosť
        self._make_label(grid, "Prenosová rýchlosť:", size=9).grid(
            row=1, column=0, sticky=tk.W, padx=(0, 8), pady=3)
        self.var_qros_baud = tk.StringVar(value=str(QROS_DEFAULT_BAUD))
        baud_combo = ttk.Combobox(
            grid, textvariable=self.var_qros_baud,
            values=[str(b) for b, _ in QROS_BAUD_CHOICES],
            state='readonly', width=10, font=("Segoe UI", 9))
        baud_combo.grid(row=1, column=1, sticky=tk.W, pady=3)
        baud_combo.bind('<<ComboboxSelected>>', lambda e: self._update_estimate())
        self._make_label(grid,
                         f"baud  ({QROS_BAUD_6595}=empirická, "
                         f"{POKEY_EXACT_6600_PAL}=POKEY PAL, "
                         f"{POKEY_EXACT_6600_NTSC}=NTSC)",
                         size=8, color=COLORS['text2']).grid(
            row=1, column=2, sticky=tk.W, padx=8, pady=3)

        # WAV
        self.var_wav = tk.BooleanVar(value=False)
        self._make_check(grid, "Generovať aj WAV", self.var_wav,
                         self._update_estimate).grid(
            row=2, column=0, columnspan=2, sticky=tk.W, pady=3)

        wav_row = tk.Frame(grid, bg=COLORS['bg2'])
        wav_row.grid(row=2, column=2, sticky=tk.W, padx=8, pady=3)
        self._make_label(wav_row, "vzorkovanie", size=8,
                         color=COLORS['text2']).pack(side=tk.LEFT)
        self.var_rate = tk.StringVar(value=str(WAV_SAMPLE_RATE))
        rate_combo = ttk.Combobox(wav_row, textvariable=self.var_rate,
                                  values=[str(r) for r in WAV_RATE_CHOICES],
                                  state='readonly', width=7, font=("Segoe UI", 9))
        rate_combo.pack(side=tk.LEFT, padx=4)
        rate_combo.bind('<<ComboboxSelected>>', lambda e: self._update_estimate())
        self._make_label(wav_row, "Hz    hlasitosť", size=8,
                         color=COLORS['text2']).pack(side=tk.LEFT)
        self.var_volume = tk.StringVar(value="61")
        ttk.Combobox(wav_row, textvariable=self.var_volume,
                     values=["25", "40", "61", "80", "100"],
                     state='readonly', width=4,
                     font=("Segoe UI", 9)).pack(side=tk.LEFT, padx=4)
        self._make_label(wav_row, "%", size=8,
                         color=COLORS['text2']).pack(side=tk.LEFT)

        # Invertovaná polarita
        self.var_invert = tk.BooleanVar(value=False)
        self._make_check(grid, "Invertovaná polarita WAV", self.var_invert).grid(
            row=3, column=0, columnspan=2, sticky=tk.W, pady=3)
        self._make_label(grid, "(prehodí úrovne signálu — skúsiť pri chybách čítania)",
                         size=8, color=COLORS['text2']).grid(
            row=3, column=2, sticky=tk.W, padx=8, pady=3)

        # Overenie
        self.var_verify = tk.BooleanVar(value=True)
        self._make_check(grid, "Overiť výstup po konverzii", self.var_verify).grid(
            row=4, column=0, columnspan=2, sticky=tk.W, pady=3)
        self.var_verify_wav = tk.BooleanVar(value=False)
        self._make_check(grid, "aj spätné dekódovanie WAV (pomalé)",
                         self.var_verify_wav).grid(
            row=4, column=2, sticky=tk.W, padx=8, pady=3)

        # Nastavovací blok
        self.var_blank = tk.BooleanVar(value=False)
        self._make_check(grid, "Úvodný nastavovací blok ($FF)",
                         self.var_blank, self._update_estimate).grid(
            row=5, column=0, columnspan=2, sticky=tk.W, pady=3)
        self._make_label(grid, "(referenčné pásky ho nemajú — zapnúť len pri potrebe)",
                         size=8, color=COLORS['text2']).grid(
            row=5, column=2, sticky=tk.W, padx=8, pady=3)

        # Odhad
        self.lbl_estimate = self._make_label(grid, "", size=9,
                                             color=COLORS['section'])
        self.lbl_estimate.grid(row=6, column=0, columnspan=3, sticky=tk.W,
                               pady=(8, 2))

        # --- Tlačidlá ---
        frm_btn = tk.Frame(tab_convert, bg=COLORS['bg'])
        frm_btn.pack(fill=tk.X, padx=12, pady=8)

        self.btn_convert = self._make_button(
            frm_btn, "  ⚡ KONVERTOVAŤ  ", self._do_convert, accent=True)
        self.btn_convert.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_analyze = self._make_button(
            frm_btn, "  Analyzovať XEX  ", self._do_analyze)
        self.btn_analyze.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_batch = self._make_button(
            frm_btn, "  Dávka...  ", self._do_batch)
        self.btn_batch.pack(side=tk.LEFT, padx=(0, 8))
        self.btn_check = self._make_button(
            frm_btn, "  Skontrolovať CAS...  ", self._do_check_cas)
        self.btn_check.pack(side=tk.LEFT)

        # Progressbar (skrytý, zobrazí sa počas konverzie)
        self.progress = ttk.Progressbar(tab_convert, mode='determinate',
                                        length=200, maximum=100)
        self.progress.pack(fill=tk.X, padx=12, pady=(0, 4))
        self.progress.pack_forget()

        # --- Log (výstupný panel) ---
        frm_log = tk.Frame(tab_convert, bg=COLORS['bg'])
        frm_log.pack(fill=tk.BOTH, expand=True, padx=12, pady=(0, 12))

        self._make_label(frm_log, "LOG", size=9, bold=True,
                         color=COLORS['section']).pack(anchor=tk.W, pady=(0, 4))

        log_wrap = tk.Frame(frm_log, bg=COLORS['bg'])
        log_wrap.pack(fill=tk.BOTH, expand=True)
        scroll = ttk.Scrollbar(log_wrap, orient=tk.VERTICAL)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)
        self.txt_log = tk.Text(log_wrap, height=10,
                               font=("Consolas", 9),
                               bg=COLORS['log_bg'], fg=COLORS['text'],
                               relief=tk.GROOVE, bd=2, wrap=tk.WORD,
                               insertbackground=COLORS['accent'],
                               selectbackground=COLORS['accent'],
                               yscrollcommand=scroll.set)
        self.txt_log.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.config(command=self.txt_log.yview)
        self.txt_log.config(state=tk.DISABLED)

        # Štýly pre farebné správy v logu
        self.txt_log.tag_configure('ok', foreground=COLORS['ok'])
        self.txt_log.tag_configure('warn', foreground=COLORS['warn'])
        self.txt_log.tag_configure('err', foreground='#c03030')
        self.txt_log.tag_configure('info', foreground=COLORS['text2'])

    def _build_info_tab(self, parent):
        """Vytvorí záložku s informáciami o systéme QROS/EMO."""
        txt = tk.Text(parent, font=("Consolas", 9),
                      bg=COLORS['log_bg'], fg=COLORS['text'],
                      relief=tk.GROOVE, bd=2, wrap=tk.WORD,
                      padx=12, pady=8)
        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=txt.yview)
        txt.config(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=8)
        txt.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        txt.tag_configure('h1', font=("Segoe UI", 14, "bold"),
                          foreground=COLORS['accent'])
        txt.tag_configure('h2', font=("Segoe UI", 11, "bold"),
                          foreground=COLORS['section'])
        txt.tag_configure('bold', font=("Consolas", 9, "bold"))
        txt.tag_configure('accent', foreground=COLORS['accent'])
        txt.tag_configure('ok', foreground=COLORS['ok'])
        txt.tag_configure('dim', foreground=COLORS['text2'])

        def h1(text):
            txt.insert(tk.END, text + "\n", 'h1')

        def h2(text):
            txt.insert(tk.END, "\n" + text + "\n", 'h2')

        def line(text, tag=None):
            txt.insert(tk.END, text + "\n", tag) if tag else \
                txt.insert(tk.END, text + "\n")

        def bold_line(label, value):
            txt.insert(tk.END, label, 'bold')
            txt.insert(tk.END, value + "\n")

        # --- Obsah ---
        h1("QROS / ROS - Rýchly Operačný Systém")
        line("")
        line("Turbo systém pre prácu s kazetou na počítačoch Atari XE/XL.")
        line("Vznikol v roku 1988 v ATARI klube v Leviciach (Slovensko).")
        line("Prvá verzia: september 1988, rýchlosť 3600 baud.")

        h2("AUTORI A KREDITY")
        bold_line("  QROS systém & SW:  ", "Ing. Matúš Žúbor")
        bold_line("  Dokumentácia:      ", "Ing. Višňovský P. (PIP Martin 02/90)")
        bold_line("                     ", "MIKRO atari č. 3-4/89, Zpravodaj AK Tlmače")
        bold_line("  HW oživenie 2025:  ", "Aleister (KiCad Rev3 PCB)")
        bold_line("  QROS Generator:    ", "W1K")
        bold_line("  Referenčný kód:    ", "Turgen System (baktra, baktra.webowna.cz)")

        h2("PRENOSOVÉ RÝCHLOSTI A POKEY DELIČE")
        line("  POKEY generuje baud rate cez celočíselný delič:")
        line("    baud = POKEY_clock / (2 x (AUDF + 7))", 'bold')
        line("")
        line("  POKEY clock: PAL = 1 773 447 Hz,  NTSC = 1 789 773 Hz")
        line("")
        line("  Deliče z originálnej dokumentácie (Ing. Žúbor, XIO 22):")
        line("  Režim    AUDF   Presný PAL    Presný NTSC   Doba (40kB)", 'bold')
        line("  ─────────────────────────────────────────────────────────")
        line(f"   6600    127    {POKEY_EXACT_6600_PAL} baud      "
             f"{POKEY_EXACT_6600_NTSC} baud      ~4min 35s")
        line(f"   9600     86    {POKEY_EXACT_9600_PAL} baud      "
             f"{POKEY_EXACT_9600_NTSC} baud      ~3min 05s")
        line(f"  32000      5    {round(pokey_baud(POKEY_AUDF_32000, POKEY_CLOCK_PAL))} baud     "
             f"{round(pokey_baud(POKEY_AUDF_32000, POKEY_CLOCK_NTSC))} baud")
        line("")
        line("  CAS baud hodnoty pre prehrávač (SIO2SD, AVR...):", 'bold')
        for baud, note in QROS_BAUD_CHOICES:
            line(f"   {baud:>5}  = {note}",
                 'ok' if baud in (QROS_BAUD_6595, QROS_BAUD_9600) else None)
        line("")
        line("  Poznámka: Označenie \"6600 baud\" bola len zaokrúhlená", 'dim')
        line("  hodnota z dokumentácie. Skutočný POKEY rate závisí od", 'dim')
        line("  PAL/NTSC a nikdy nie je presne 6600.", 'dim')
        line("")
        line("  Originálna dokumentácia uvádzala aj:", 'dim')
        line("   19200 baud (~1min 35s), 12800 baud (~2min 15s)", 'dim')
        line("   4800 baud, 3600 baud (prvá verzia, september 1988)", 'dim')

        h2("ŠTRUKTÚRA BLOKU (132 bajtov)")
        line("  Kazetový buffer: $3FB - $47F (131 bajtov + riadiaci)")
        line("")
        line("  Bajt   Význam", 'bold')
        line("  ─────────────────────────────────────────────────────")
        line("  [0]    Index bloku (vysoký bajt)")
        line("  [1]    Index bloku (nízky bajt)")
        line("  [2]    Typ bloku (riadiaci bajt)")
        line("  [3-130] 128 dátových bajtov")
        line("  [131]  SIO kontrolný súčet (súčet s carry)")
        line("")
        line("  Synchronizačné bajty: $55 (01010101) - striedané bity")

        h2("TYPY BLOKOV")
        line("  Kód    Typ              Popis", 'bold')
        line("  ─────────────────────────────────────────────────────")
        line("  $FF    Nastavovací      Prázdny blok (všetky bajty $FF)")
        line("  $FD    Informačný       Hlavička s názvom a boot loaderom")
        line("  $FC    Dátový úplný     128 bajtov platných dát")
        line("  $FA    Dátový neúplný   Menej ako 128B, posledný = počet platných")
        line("  $FE    Koncový          Signalizuje koniec súboru")

        h2("PORADIE ZÁZNAMOV NA PÁSKE")
        line(f"  IRG {QROS_IRG_HEADER} ms  →  hlavička ($FD, index 1)")
        line(f"  IRG {QROS_IRG_HEADER_REPEAT} ms    →  hlavička (identická kópia)")
        line(f"  IRG {QROS_IRG_DATA} ms    →  dátový blok index 2")
        line("  ...")
        line(f"  IRG {QROS_IRG_DATA} ms    →  koncový blok ($FE)")
        line("")
        line("  Overené proti referenčnej páske Decathlon.cas —", 'ok')
        line("  generátor vytvára bit-for-bit zhodný obraz.", 'ok')

        h2("NÁZOV NA PÁSKE")
        line(f"  Offset {QROS_NAME_OFFSET:#04x}–0x20 v hlavičke = {QROS_NAME_LENGTH} znakov ATASCII,")
        line(f"  doplnené medzerami, za nimi 0x{QROS_NAME_EOL:02X} (ATASCII EOL).")
        line("  Diakritika sa prevádza na základné písmená (Ž→Z, č→c),")
        line("  lebo ATASCII znaková sada slovenské znaky nemá.")

        h2("PRINCÍP ZÁZNAMU - MANCHESTER KÓDOVANIE")
        line("  POKEY signály: CLKIN, CLKOUT, DATAIN, DATAOUT (TTL úrovne)")
        line("")
        line("  Fázový spôsob záznamu (Manchester / biphase-L):")
        line("    - Každý bit má prechod v strede bitového intervalu")
        line("    - Bit 0: vysoká → nízka úroveň")
        line("    - Bit 1: nízka → vysoká úroveň")
        line("    - Samosynchronizujúce - hodiny sú zakódované v dátach")
        line("")
        line("  SIO rámec: 1 štart bit (0) + 8 dátových (LSB) + 1 stop bit (1)")
        line("  → 10 bitov na bajt, 1320 bitov na 132-bajtový blok")

        h2("BOOT PROCES")
        line("  1. Stlačiť START + OPTION pri zapnutí / RESETe")
        line("  2. Zavádžač ROS sa nahrá z kazety")
        line("  3. Po nahratí ponúkne režimy:")
        line("       B - skok do BASIC-u (plná inicializácia)")
        line("       S - bootovanie s ROM BASIC (START)")
        line("       L - bootovanie strojových programov (START+OPTION)")

        h2("QROS vs ROS")
        line("  ROS  = pôvodný systém pre Atari 800XL (64kB)")
        line("  QROS = rozšírená verzia pre Atari 130XE (128kB)")
        line("         - pridaný RAMDISK využívajúci rozšírenú pamäť")
        line("         - názov z \"Q1\" typu zariadenia v OS")

        h2("HARDVÉR (Aleister Rev3, 2025)")
        line("  Čipy: 4049, 74LS86 (XOR), 74LS121 (MKO),")
        line("         74LS74 (D flip-flop), 74LS157 (multiplexer)")
        line("         MH3ST2 (Schmidtov klopný obvod), BC238")
        line("  Vstup z mgf: 300-1500 mV")
        line("  Kompatibilné: QROS 9600, Turbo D (12000 Bd), Turbo 2000 CZ")
        line("  PCB: 125 x 38 mm, Open Hardware, navrhnuté v KiCad")

        h2("FORMÁT CAS SÚBORU")
        line("  Chunk: [4B ID][2B dĺžka LE][2B aux LE][dáta]")
        line("")
        line("  FUJI  - identifikácia CAS súboru (popis)")
        line("  baud  - prenosová rýchlosť (aux = baudrate)")
        line("  data  - dátový záznam (aux = IRG v ms)")
        line("")
        line("  Pozor: štandardné CAS 'data' chunky bežné emulátory (Altirra)", 'dim')
        line("  prehrávajú ako FSK modulovaný SIO záznam, nie ako Manchester.", 'dim')
        line("  CAS je preto určený pre QROS-aware prehrávač (SIO2SD, AVR emulátor),", 'dim')
        line("  ktorý si signál vyrobí sám. Pre priame prehratie do reálneho", 'dim')
        line("  QROS hardvéru z linkového výstupu použite WAV.", 'dim')

        h2("CAS vs WAV")
        line("  Obe cesty čítajú z tej istej sekvencie blokov, takže sa")
        line("  nemôžu rozísť. CAS = predpis (čo hrať), WAV = hotová nahrávka.")

        txt.config(state=tk.DISABLED)

    def _build_changelog_tab(self, parent):
        """Vytvorí záložku s históriou zmien."""
        txt = tk.Text(parent, font=("Consolas", 9),
                      bg=COLORS['log_bg'], fg=COLORS['text'],
                      relief=tk.GROOVE, bd=2, wrap=tk.WORD,
                      padx=12, pady=8)
        scroll = ttk.Scrollbar(parent, orient=tk.VERTICAL, command=txt.yview)
        txt.config(yscrollcommand=scroll.set)
        scroll.pack(side=tk.RIGHT, fill=tk.Y, pady=8)
        txt.pack(fill=tk.BOTH, expand=True, padx=12, pady=8)

        txt.tag_configure('h1', font=("Segoe UI", 14, "bold"),
                          foreground=COLORS['accent'])
        txt.tag_configure('h2', font=("Segoe UI", 11, "bold"),
                          foreground=COLORS['section'])
        txt.tag_configure('dim', foreground=COLORS['text2'])

        def h1(text):
            txt.insert(tk.END, text + "\n", 'h1')

        def h2(text):
            txt.insert(tk.END, "\n" + text + "\n", 'h2')

        def line(text, tag=None):
            txt.insert(tk.END, text + "\n", tag) if tag else \
                txt.insert(tk.END, text + "\n")

        h1(f"{APP_NAME} - Changelog")

        h2("v0.5 — 2026-08-27")
        line("  Formát výstupu je nezmenený — overené bit-for-bit proti", 'dim')
        line("  referenčnej páske Decathlon.cas.", 'dim')
        line("")
        line("  Opravy:")
        line("  - Chyba pri konverzii sa už zobrazí správne. Doteraz lambda")
        line("    v except vetve odkazovala na premennú, ktorú Python po")
        line("    ukončení bloku maže — namiesto hlásenia spadol callback a")
        line("    GUI zostalo zamknuté s točiacim sa progressbarom.")
        line("  - Názov na páske sa prevádza do ATASCII (Ž→Z, č→c). Doteraz")
        line("    sa robilo ord(znak) & 0x7F, čo zo slovenskej diakritiky")
        line("    vyrobilo náhodné znaky (š → a).")
        line("  - XEX parser hlási skrátené segmenty, zvyšné bajty a chýbajúcu")
        line("    RUN/INIT adresu namiesto tichého ignorovania.")
        line("  - Kontrola rozsahov pri tvorbe blokov (index, počet bajtov).")
        line("  - WAV zápis je korektný aj na big-endian procesore.")
        line("")
        line("  Nové:")
        line("  - Overenie výstupu: CAS sa po zápise rozloží späť, skontrolujú")
        line("    sa chunky, indexy, typy, IRG a checksumy a dáta sa porovnajú")
        line("    s pôvodným XEX.")
        line("  - Spätný Manchester dekodér — voliteľne prečíta vygenerovaný")
        line("    WAV a porovná ho s originálom.")
        line("  - Príkazový riadok (argparse): dávková konverzia, --wav,")
        line("    --baud, --rate, --volume, --invert, --info, --verify-wav.")
        line("  - Dávková konverzia viacerých XEX naraz priamo z GUI.")
        line("  - Tlačidlo \"Skontrolovať CAS...\" na kontrolu hotového obrazu.")
        line("  - Voliteľné vzorkovanie WAV (44.1 / 48 / 96 / 192 kHz)")
        line("    a hlasitosť signálu.")
        line("  - Voliteľný úvodný nastavovací blok ($FF).")
        line("  - Odhad počtu blokov, dĺžky pásky a veľkosti WAV pred konverziou.")
        line("  - Skutočný priebeh konverzie v progressbare.")
        line("")
        line("  Vnútorné:")
        line("  - CAS aj WAV čítajú z jedného generátora záznamov")
        line("    (iter_qros_records) — nemôžu sa už rozísť.")
        line("  - WAV sa zapisuje prúdovo, nedrží sa celý v pamäti.")
        line("  - Manchester kodér ako trieda s nastaviteľným vzorkovaním.")

        h2("v0.4 — 2026-03-05")
        line("  - Optimalizovaný WAV generátor (blokové array operácie namiesto")
        line("    Python cyklu po vzorkách — rádovo 10-20x rýchlejšie)")
        line("  - Konverzia v samostatnom vlákne (GUI nezamrzne, progressbar)")
        line("  - Interné dáta cez bytearray namiesto list (menšia spotreba RAM)")

        h2("v0.3 — 2026-03-05")
        line("  - WAV vzorkovacia frekvencia zvýšená na 96 kHz (z 44.1 kHz)")
        line("  - Pridaná voľba invertovanej polarity WAV signálu")
        line("  - Pridaný Changelog tab priamo v programe")

        h2("v0.2 — 2026-03-05 19:57")
        line("  - Odstránený Turbo 2000 (ponechaný len QROS/EMO)")
        line("  - Odstránená analýza CAS súborov")
        line("  - Pridaný WAV generátor (Manchester kódovanie, 44100 Hz, 16-bit PCM)")
        line("  - Pridaný Info tab s kompletnou dokumentáciou systému QROS")
        line("  - Pridané POKEY deliče z originálnej dokumentácie (Ing. Žúbor, XIO 22)")
        line("  - Výpočet presných POKEY baud rates pre PAL a NTSC")
        line("  - Rozšírený combo box prenosových rýchlostí (6 hodnôt)")
        line("  - GUI prerobené na záložkový systém (Konverzia + Info)")
        line("  - Program premenovaný na QROS Generator")
        line("  - Slovenské komentáre a GUI texty")
        line("  - Kredity: Ing. M. Žúbor, Aleister, W1K, Turgen/baktra")

        h2("v0.1 — 2026-03-05 19:00")
        line("  - Prvá verzia (xex2cas.py)")
        line("  - Podpora Turbo 2000 (PWM) a QROS/EMO (Manchester)")
        line("  - CAS analýza")
        line("  - Tkinter GUI s výberom turbo systému")
        line("  - Anglické rozhranie")

        txt.config(state=tk.DISABLED)

    # ---- Logovanie ----

    def _log(self, msg, tag=None):
        """Pridá správu do log panelu (len z hlavného vlákna)."""
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.insert(tk.END, msg + "\n", tag) if tag else \
            self.txt_log.insert(tk.END, msg + "\n")
        self.txt_log.see(tk.END)
        self.txt_log.config(state=tk.DISABLED)

    def _log_threadsafe(self, msg, tag=None):
        """Log volateľný z pracovného vlákna."""
        self.root.after(0, lambda m=msg, t=tag: self._log(m, t))

    def _log_clear(self):
        """Vymaže obsah log panelu."""
        self.txt_log.config(state=tk.NORMAL)
        self.txt_log.delete("1.0", tk.END)
        self.txt_log.config(state=tk.DISABLED)

    # ---- Odhad výstupu ----

    def _current_options(self):
        """Zozbiera nastavenia z GUI."""
        volume = int(self.var_volume.get())
        return ConversionOptions(
            baudrate=int(self.var_qros_baud.get()),
            make_wav=self.var_wav.get(),
            sample_rate=int(self.var_rate.get()),
            amplitude=int(32767 * volume / 100),
            invert=self.var_invert.get(),
            leading_blank=self.var_blank.get(),
            verify=self.var_verify.get(),
            verify_wav_data=self.var_verify_wav.get(),
            tape_name=self.var_name.get().strip() or None)

    def _update_estimate(self, *_args):
        """Prepočíta odhad dĺžky pásky a veľkosti výstupu."""
        if self.xex is None:
            self.lbl_estimate.config(text="Vyberte vstupný súbor…")
            return
        try:
            opts = self._current_options()
        except (ValueError, tk.TclError):
            return
        size = self.xex.size
        plan = qros_plan(size, opts.leading_blank)
        dur = qros_tape_duration(size, opts.baudrate, opts.leading_blank)
        text = (f"{plan['records']} záznamov "
                f"({plan['blocks_complete']} úplných + "
                f"{plan['blocks_partial']} neúplných + EOF), "
                f"dĺžka na páske ~{format_duration(dur)}")
        if opts.make_wav:
            wav_bytes = int((dur + WAV_TAIL_MS / 1000.0)
                            * opts.sample_rate * 2) + 44
            text += f", WAV ~{format_size(wav_bytes)}"
        self.lbl_estimate.config(text=text)

    # ---- Výber súborov ----

    def _browse_input(self):
        """Dialóg na výber vstupného XEX súboru."""
        path = filedialog.askopenfilename(
            title="Vybrať XEX/COM súbor",
            filetypes=[
                ("Atari spustiteľné", "*.xex *.com *.exe *.obx"),
                ("Všetky súbory", "*.*"),
            ])
        if path:
            self.var_input.set(path)
            self.var_output.set(os.path.splitext(path)[0] + ".cas")
            fname = os.path.splitext(os.path.basename(path))[0]
            self.var_name.set(fname[:QROS_NAME_LENGTH])
            self._do_analyze()

    def _browse_output(self):
        """Dialóg na uloženie výstupného CAS súboru."""
        path = filedialog.asksaveasfilename(
            title="Uložiť CAS súbor", defaultextension=".cas",
            filetypes=[("CAS kazetový obraz", "*.cas"), ("Všetky súbory", "*.*")])
        if path:
            self.var_output.set(path)

    # ---- Analýza XEX súboru ----

    def _do_analyze(self):
        """Analyzuje vstupný XEX súbor a zobrazí informácie v logu."""
        self._log_clear()
        input_path = self.var_input.get().strip()
        if not input_path:
            self._log("Nie je vybraný vstupný súbor.", 'warn')
            return
        if not os.path.isfile(input_path):
            self._log(f"Súbor nenájdený: {input_path}", 'err')
            return

        try:
            self.xex = XEXFile(input_path)
        except Exception as e:
            self._log(f"Chyba parseru: {e}", 'err')
            self.xex = None
            self._update_estimate()
            return

        self._log(f"Súbor: {os.path.basename(input_path)} "
                  f"({self.xex.size} bajtov)", 'info')
        self._log(f"Segmenty: {len(self.xex.segments)}")
        for seg in self.xex.segments:
            self._log(f"   {seg}")

        if self.xex.run_address is not None:
            self._log(f"RUN:  ${self.xex.run_address:04X}")
        else:
            self._log("RUN:  nie je nastavená", 'warn')
        for addr in self.xex.init_addresses:
            self._log(f"INIT: ${addr:04X}")

        for w in self.xex.warnings:
            self._log(f"Poznámka: {w}", 'warn')

        self._update_estimate()
        self._log("Pripravené na konverziu.", 'ok')

    # ---- Kontrola hotového CAS ----

    def _do_check_cas(self):
        """Skontroluje existujúci CAS súbor (štruktúra, indexy, checksumy)."""
        path = filedialog.askopenfilename(
            title="Vybrať CAS súbor na kontrolu",
            filetypes=[("CAS kazetový obraz", "*.cas"), ("Všetky súbory", "*.*")])
        if not path:
            return
        self._log_clear()
        self._log(f"Kontrola: {os.path.basename(path)}", 'info')
        try:
            with open(path, 'rb') as f:
                data = f.read()
            chunks = parse_cas(data)
        except Exception as e:
            self._log(f"CHYBA: {e}", 'err')
            return

        for cid, aux, body in chunks:
            if cid == b'FUJI':
                self._log(f"  FUJI: {body.decode('latin1')}")
            elif cid == b'baud':
                self._log(f"  baud: {aux}")
        data_blocks = [c[2] for c in chunks if c[0] == b'data']
        types = {}
        for b in data_blocks:
            if len(b) == QROS_RECORD_SIZE:
                types[b[2]] = types.get(b[2], 0) + 1
        for t, count in sorted(types.items()):
            self._log(f"  ${t:02X} {QROS_BLOCK_NAMES.get(t, 'neznámy'):22} {count}x")

        ok, msgs = verify_cas(data)
        for m in msgs:
            self._log(f"  {m}", 'ok' if ok else 'err')
        payload, _ = blocks_to_payload(data_blocks)
        self._log(f"  Dáta v obraze: {len(payload)} bajtov "
                  f"(hlavička: {payload[:2].hex(' ').upper() if payload else '—'})")
        self._log("Obraz je v poriadku." if ok else "Obraz obsahuje chyby!",
                  'ok' if ok else 'err')

    # ---- Konverzia ----

    def _set_busy(self, busy, total=0):
        """Zamkne / odomkne GUI počas práce."""
        self.busy = busy
        state = tk.DISABLED if busy else tk.NORMAL
        for btn in (self.btn_convert, self.btn_analyze,
                    self.btn_batch, self.btn_check):
            btn.config(state=state)
        if busy:
            self.progress['value'] = 0
            self.progress['maximum'] = max(1, total)
            self.progress.pack(fill=tk.X, padx=12, pady=(0, 4))
        else:
            self.progress.pack_forget()

    def _progress_cb(self, done, total):
        """Callback z pracovného vlákna — prekreslí progressbar."""
        if done % 16 == 0 or done == total:
            self.root.after(0, lambda d=done, t=total: self._set_progress(d, t))

    def _set_progress(self, done, total):
        self.progress['maximum'] = max(1, total)
        self.progress['value'] = done

    def _run_worker(self, jobs, opts):
        """Spustí konverziu zoznamu (vstup, výstup) v pracovnom vlákne."""
        total_records = 0
        for input_path, _out in jobs:
            try:
                size = os.path.getsize(input_path)
                records = qros_plan(size, opts.leading_blank)['records']
                total_records += records * (2 if opts.make_wav else 1)
            except OSError:
                pass

        self._set_busy(True, total_records)
        state = {'done': 0}

        def progress(done, total):
            # done/total sa počíta v rámci jedného súboru — spočítame globálne
            state['done'] += 1
            self._progress_cb(state['done'], max(1, total_records))

        def worker():
            results = []
            errors = []
            for input_path, output_path in jobs:
                try:
                    self._log_threadsafe(
                        f"── {os.path.basename(input_path)}", 'info')
                    result = convert_file(input_path, output_path, opts,
                                          log=self._log_threadsafe,
                                          progress=progress)
                    results.append(result)
                except Exception as e:
                    msg = f"{os.path.basename(input_path)}: {e}"
                    errors.append(msg)
                    self._log_threadsafe(f"CHYBA: {msg}", 'err')
            self.root.after(0, lambda: self._work_done(results, errors))

        threading.Thread(target=worker, daemon=True).start()

    def _work_done(self, results, errors):
        """Callback po dokončení práce (hlavné vlákno)."""
        self._set_busy(False)

        if errors and not results:
            messagebox.showerror("Chyba", "\n".join(errors))
            return

        lines = []
        problems = 0
        for r in results:
            if r.get('cas_path'):
                lines.append(f"{os.path.basename(r['cas_path'])} "
                             f"({format_size(r['cas_size'])})")
            if r.get('wav_path'):
                lines.append(f"{os.path.basename(r['wav_path'])} "
                             f"({format_size(r['wav_size'])}, "
                             f"{r['wav_info']['wav_duration_s']:.1f} s)")
            if r.get('cas_ok') is False or r.get('wav_ok') is False:
                problems += 1

        summary = f"Hotovo — {len(results)} súbor(ov).\n\n" + "\n".join(lines)
        if errors:
            summary += "\n\nChyby:\n" + "\n".join(errors)
        if problems:
            summary += f"\n\nPOZOR: {problems} výstup(ov) neprešlo overením!"
            messagebox.showwarning("Hotovo s výhradami", summary)
        elif errors:
            messagebox.showwarning("Hotovo s chybami", summary)
        else:
            messagebox.showinfo("Hotovo", summary)

    def _do_convert(self):
        """Spustí konverziu jedného XEX -> QROS CAS."""
        if self.busy:
            return
        input_path = self.var_input.get().strip()
        output_path = self.var_output.get().strip()

        if not input_path:
            messagebox.showerror("Chyba", "Nie je vybraný vstupný súbor.")
            return
        if not output_path:
            messagebox.showerror("Chyba", "Nie je zadaný výstupný súbor.")
            return
        if not os.path.isfile(input_path):
            messagebox.showerror("Chyba", f"Vstupný súbor nenájdený:\n{input_path}")
            return
        if os.path.abspath(input_path) == os.path.abspath(output_path):
            messagebox.showerror("Chyba", "Výstup by prepísal vstupný súbor.")
            return

        if self.xex is None or self.xex.filename != input_path:
            self._do_analyze()
        if self.xex is None:
            return

        self._run_worker([(input_path, output_path)], self._current_options())

    def _do_batch(self):
        """Dávková konverzia viacerých súborov naraz."""
        if self.busy:
            return
        paths = filedialog.askopenfilenames(
            title="Vybrať XEX/COM súbory na dávkovú konverziu",
            filetypes=[
                ("Atari spustiteľné", "*.xex *.com *.exe *.obx"),
                ("Všetky súbory", "*.*"),
            ])
        if not paths:
            return

        opts = self._current_options()
        opts.tape_name = None            # každý súbor si nesie vlastný názov
        jobs = [(p, os.path.splitext(p)[0] + ".cas") for p in paths]

        self._log_clear()
        self._log(f"Dávková konverzia: {len(jobs)} súborov", 'info')
        self._run_worker(jobs, opts)

    # ---- Spustenie ----

    def run(self):
        """Spustí hlavnú slučku GUI."""
        self.root.mainloop()


# =============================================================================

def main():
    code = main_cli(sys.argv[1:])
    if code is not None:
        return code
    if not HAVE_TK:
        cli_log("tkinter nie je dostupné — použite príkazový riadok "
                "(qros_generator-v0.5.py --help)", 'err')
        return 2
    XEX2CASApp().run()
    return 0


if __name__ == '__main__':
    sys.exit(main())
